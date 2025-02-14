import math
from pathlib import Path
from typing import Any
from typing import Callable
from typing import Iterator
from typing import Literal
from typing import Optional
from typing import Sequence
from typing import Union

import cupy as cp
import numpy as np
import pandas as pd

from bigwig_loader.collection import BigWigCollection
from bigwig_loader.cupy_functions import moving_average
from bigwig_loader.genome import Genome
from bigwig_loader.position_sampler import PositionSampler


class BigWigDataset:
    """
    Dataset over FASTA files and BigWig profiles.
    Args:
        regions_of_interest: pandas dataframe with intervals within the chromosomes to
            sample from. These intervals are not intervals of sequence_length
            but regions of the chromosome that were selected up front. For
            instance regions in which called peaks are located or a threshold
            value is exceeded.
        collection: instance of bigwig_loader.BigWigCollection or path to BigWig
            Directory or list of BigWig files.
        reference_genome_path: path to fasta file containing the reference genome.
        sequence_length: number of base pairs in input sequence
        center_bin_to_predict: if given, only do prediction on a central window. Should be
            smaller than or equal to sequence_length. If not given will be the same as
            sequence_length.
        window_size: used to down sample the resolution of the target from sequence_length
        moving_average_window_size: window size for moving average on the target. Can
            help too smooth out the target. Default: 1, which means no smoothing. If
            used in combination with window_size, the target is first downsampled and
            then smoothed.
        batch_size: batch size
        super_batch_size: batch size that is used in the background to load data from
            bigwig files. Should be larger than batch_size. If None, it will be equal to
            batch_size.
        batches_per_epoch: because the length of an epoch is slightly arbitrary here,
            the number of batches can be set by hand. If not the number of batches per
            epoch will be (totol number of bases in combined intervals) // sequence_length // batch_size
        maximum_unknown_bases_fraction: maximum number of bases in an input sequence that
            is unknown.
        sequence_encoder: encoder to apply to the sequence. Default: bigwig_loader.util.onehot_sequences
        file_extensions: load files with these extensions (default .bw and .bigWig)
        crawl: whether to search in sub-directories for BigWig files
        scale: Optional, dictionary with scaling factors for each BigWig file.
            If None, no scaling is done. Keys can be (partial) file paths. See
            bigwig_loader.path.match_key_to_path for more information about how
            dict keys are mapped to paths.
        first_n_files: Only use the first n files (handy for debugging on less tasks)
        position_sampler_buffer_size: number of intervals picked up front by the position sampler.
            When all intervals are used, new intervals are picked.
        repeat_same_positions: if True the positions sampler does not draw a new random collection
            of positions when the buffer runs out, but repeats the same samples. Can be used to
            check whether network can overfit.
        use_cufile: whether to use kvikio cuFile to directly load data from file to GPU memory.

    """

    def __init__(
        self,
        regions_of_interest: pd.DataFrame,
        collection: Union[str, Sequence[str], Path, Sequence[Path], BigWigCollection],
        reference_genome_path: Path,
        sequence_length: int = 1000,
        center_bin_to_predict: Optional[int] = None,
        window_size: int = 1,
        moving_average_window_size: int = 1,
        batch_size: int = 256,
        super_batch_size: Optional[int] = None,
        batches_per_epoch: Optional[int] = None,
        maximum_unknown_bases_fraction: float = 0.1,
        sequence_encoder: Optional[
            Union[Callable[[Sequence[str]], Any], Literal["onehot"]]
        ] = "onehot",
        file_extensions: Sequence[str] = (".bigWig", ".bw"),
        crawl: bool = True,
        scale: Optional[dict[Union[str | Path], Any]] = None,
        first_n_files: Optional[int] = None,
        position_sampler_buffer_size: int = 100000,
        repeat_same_positions: bool = False,
        use_cufile: bool = True,
    ):
        self.batch_size = batch_size
        super_batch_size = super_batch_size or batch_size
        if super_batch_size < batch_size:
            raise AttributeError(
                f"super_batch_size {batch_size} can not be smaller than batch_size {batch_size}"
            )
        self.batches_per_epoch = (
            batches_per_epoch
            or (regions_of_interest["end"] - regions_of_interest["start"]).sum()
            // sequence_length
            // batch_size
        )

        super_batches_per_epoch = math.ceil(
            batch_size * self.batches_per_epoch / super_batch_size
        )

        self._super_dataset = BigWigSuperDataset(
            regions_of_interest=regions_of_interest,
            collection=collection,
            reference_genome_path=reference_genome_path,
            sequence_length=sequence_length,
            center_bin_to_predict=center_bin_to_predict,
            window_size=window_size,
            moving_average_window_size=moving_average_window_size,
            batch_size=super_batch_size,
            batches_per_epoch=super_batches_per_epoch,
            maximum_unknown_bases_fraction=maximum_unknown_bases_fraction,
            sequence_encoder=sequence_encoder,
            file_extensions=file_extensions,
            crawl=crawl,
            scale=scale,
            first_n_files=first_n_files,
            position_sampler_buffer_size=position_sampler_buffer_size,
            repeat_same_positions=repeat_same_positions,
            use_cufile=use_cufile,
        )
        self._super_batch_sequences: cp.ndarray = None
        self._super_batch_targets: cp.ndarray = None
        self._n = 0
        self._offset = 0

    def reset_gpu(self) -> None:
        self._super_dataset.reset_gpu()

    def __iter__(self) -> Iterator[tuple[Any, cp.ndarray]]:
        self._n = 0
        self._offset = 0
        iter(self._super_dataset)
        return self

    def __next__(self) -> tuple[Any, cp.ndarray]:
        if self._n < self.batches_per_epoch:
            self._n += 1
            if (
                self._super_batch_sequences is None
                or self._super_batch_targets is None
                or self._offset + self.batch_size > self._super_dataset.batch_size
            ):
                self._super_batch_sequences, self._super_batch_targets = next(
                    self._super_dataset
                )
                self._offset = 0

            sequences = self._super_batch_sequences[
                self._offset : self._offset + self.batch_size
            ]
            target = self._super_batch_targets[
                self._offset : self._offset + self.batch_size
            ]
            self._offset += self.batch_size
            return sequences, target

        raise StopIteration


class BigWigSuperDataset:
    """
    Dataset over FASTA files and BigWig profiles.
    Args:
        regions_of_interest: pandas dataframe with intervals within the chromosomes to
            sample from. These intervals are not intervals of sequence_length
            but regions of the chromosome that were selected up front. For
            instance regions in which called peaks are located or a threshold
            value is exceeded.
        collection: instance of bigwig_loader.BigWigCollection or path to BigWig
            Directory or list of BigWig files.
        reference_genome_path: path to fasta file containing the reference genome.
        sequence_length: number of base pairs in input sequence
        center_bin_to_predict: if given, only do prediction on a central window. Should be
            smaller than or equal to sequence_length. If not given will be the same as
            sequence_length.
        window_size: used to down sample the resolution of the target from sequence_length
        moving_average_window_size: window size for moving average on the target. Can
            help too smooth out the target. Default: 1, which means no smoothing. If
            used in combination with window_size, the target is first downsampled and
            then smoothed.
        batch_size: batch size
        batches_per_epoch: because the length of an epoch is slightly arbitrary here,
            the number of batches can be set by hand. If not the number of batches per
            epoch will be (totol number of bases in combined intervals) // sequence_length // batch_size
        maximum_unknown_bases_fraction: maximum number of bases in an input sequence that
            is unknown.
        sequence_encoder: encoder to apply to the sequence. Default: bigwig_loader.util.onehot_sequences
        file_extensions: load files with these extensions (default .bw and .bigWig)
        crawl: whether to search in sub-directories for BigWig files
        scale: Optional, dictionary with scaling factors for each BigWig file.
            If None, no scaling is done. Keys can be (partial) file paths. See
            bigwig_loader.path.match_key_to_path for more information about how
            dict keys are mapped to paths.
        first_n_files: Only use the first n files (handy for debugging on less tasks)
        position_sampler_buffer_size: number of intervals picked up front by the position sampler.
            When all intervals are used, new intervals are picked.
        repeat_same_positions: if True the positions sampler does not draw a new random collection
            of positions when the buffer runs out, but repeats the same samples. Can be used to
            check whether network can overfit.
        use_cufile: whether to use kvikio cuFile to directly load data from file to GPU memory.
    """

    def __init__(
        self,
        regions_of_interest: pd.DataFrame,
        collection: Union[str, Sequence[str], Path, Sequence[Path], BigWigCollection],
        reference_genome_path: Path,
        sequence_length: int = 1000,
        center_bin_to_predict: Optional[int] = None,
        window_size: int = 1,
        moving_average_window_size: int = 1,
        batch_size: int = 256,
        batches_per_epoch: Optional[int] = None,
        maximum_unknown_bases_fraction: float = 0.1,
        sequence_encoder: Optional[
            Union[Callable[[Sequence[str]], Any], Literal["onehot"]]
        ] = "onehot",
        file_extensions: Sequence[str] = (".bigWig", ".bw"),
        crawl: bool = True,
        scale: Optional[dict[Union[str | Path], Any]] = None,
        first_n_files: Optional[int] = None,
        position_sampler_buffer_size: int = 100000,
        repeat_same_positions: bool = False,
        use_cufile: bool = True,
    ):
        super().__init__()

        self.regions_of_interest = regions_of_interest
        if isinstance(collection, BigWigCollection):
            self._bigwig_collection: Optional[BigWigCollection] = collection
            self._bigwig_path: Optional[
                Union[str, Sequence[str], Path, Sequence[Path]]
            ] = None
        else:
            self._bigwig_collection = None
            self._bigwig_path = collection
        self.reference_genome_path = reference_genome_path
        self.sequence_length = sequence_length
        if center_bin_to_predict:
            self.center_bin_to_predict = center_bin_to_predict
        else:
            self.center_bin_to_predict = sequence_length
        self.window_size = window_size
        self.batch_size = batch_size
        self.batches_per_epoch = (
            batches_per_epoch
            or (regions_of_interest["end"] - regions_of_interest["start"]).sum()
            // sequence_length
            // batch_size
        )
        self._n = 0
        self.maximum_unknown_bases_fraction = maximum_unknown_bases_fraction
        self.sequence_encoder = sequence_encoder
        self._first_n_files = first_n_files
        self._file_extensions = file_extensions
        self._crawl = crawl
        self._scale = scale
        self._genome: Optional[Genome] = None
        self._prepared_out: Optional[cp.ndarray] = None
        self._position_sampler_buffer_size = position_sampler_buffer_size
        self._repeat_same_positions = repeat_same_positions
        self._moving_average_window_size = moving_average_window_size
        self._use_cufile = use_cufile

    def reset_gpu(self) -> None:
        self.bigwig_collection.reset_gpu()

    @property
    def genome(self) -> Genome:
        """
        Setup genome object to get sequences from the reference genome.
        """
        if not self._genome:
            position_sampler = PositionSampler(
                regions_of_interest=self.regions_of_interest,
                buffer_size=self._position_sampler_buffer_size,
                repeat_same=self._repeat_same_positions,
            )
            self._genome = Genome(
                self.reference_genome_path,
                sequence_length=self.sequence_length,
                position_sampler=position_sampler,
                batch_size=self.batch_size,
                maximum_unknown_bases_fraction=self.maximum_unknown_bases_fraction,
                encoder=self.sequence_encoder,
            )
        return self._genome

    @property
    def bigwig_collection(self) -> BigWigCollection:
        """
        Setup the BigWigCollection to get values from all bigwig
        files from
        """

        if self._bigwig_collection is not None:
            return self._bigwig_collection

        elif self._bigwig_path is not None:
            self._bigwig_collection = BigWigCollection(
                self._bigwig_path,
                file_extensions=self._file_extensions,
                crawl=self._crawl,
                scale=self._scale,
                first_n_files=self._first_n_files,
                use_cufile=self._use_cufile,
            )
            return self._bigwig_collection
        else:
            raise RuntimeError(
                f"{self}._bigwig_collection and {self}._bigwig_path are bot None. At least one should be set."
            )

    @property
    def _out(self) -> cp.ndarray:
        if self._prepared_out is None:
            self._prepared_out = cp.zeros(
                (
                    len(self.bigwig_collection),
                    self.batch_size,
                    self.center_bin_to_predict // self.window_size,
                ),
                dtype=cp.float32,
            )
        return self._prepared_out

    def __iter__(self) -> Iterator[tuple[Any, cp.ndarray]]:
        self._n = 0
        return self

    def __next__(self) -> tuple[Any, cp.ndarray]:
        if self._n < self.batches_per_epoch:
            self._n += 1
            positions, sequences = self.genome.get_batch()
            chromosomes, center = zip(*positions)
            start = np.array(center) - (self.center_bin_to_predict // 2)
            end = start + self.center_bin_to_predict
            target = self.bigwig_collection.get_batch(
                chromosomes,
                start,
                end,
                window_size=self.window_size,
                out=self._out,
            )
            target = moving_average(target, self._moving_average_window_size)
            return sequences, target
        raise StopIteration
