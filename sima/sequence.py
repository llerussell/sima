"""
Sequence Objects
================

Within SIMA, imaging data is contained in Sequence objects.
"""

# ImagingDataset objects must be initialized with a list of
# `iterable <http://docs.python.org/2/glossary.html#term-iterable>`_
# objects that satisfy the following properties:
#
# * The iterable should not be its own iterator, i.e. it should be able to
#   spawn multiple iterators that can be iterated over independently.
# * Each iterator spawned from the iterable must yield image frames in the form
#   of numpy arrays with shape (num_rows, num_columns).
# * Iterables must survive pickling and unpickling.
#
# Examples of valid sequence include:
#
# * numpy arrays of shape (num_frames, num_rows, num_columns)
#
#   >>> import sima
#   >>> from numpy import ones
#   >>> frames = ones((100, 128, 128))
#   >>> sima.ImagingDataset([[frames]], None)
#   <ImagingDataset: num_channels=1, num_cycles=1, frame_size=128x128,
#   num_frames=100>
#
# * lists of numpy arrays of shape (num_rows, num_columns)
#
#   >>> frames = [ones((128, 128)) for _ in range(100)]
#   >>> sima.ImagingDataset([[frames]], None)
#   <ImagingDataset: num_channels=1, num_cycles=1, frame_size=128x128,
#   num_frames=100>
#
# For convenience, we have created iterable objects that can be used with
# common data formats.

import itertools
import warnings
from distutils.version import StrictVersion
from os.path import (abspath, dirname, join, normpath, normcase, isfile,
                     samefile)
from abc import ABCMeta, abstractmethod

import numpy as np

# try:
#     from libtiff import TIFF
#     libtiff_available = True
# except ImportError:
#     with warnings.catch_warnings():
#         warnings.simplefilter("ignore")
#         from sima.misc.tifffile import TiffFile
#     libtiff_available = False
try:
    import h5py
except ImportError:
    h5py_available = False
else:
    h5py_available = StrictVersion(h5py.__version__) >= StrictVersion('2.3.1')

import sima.misc
from sima._motion import _align_frame
with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from sima.misc.tifffile import TiffFileWriter


class Sequence(object):

    """A sequence contains the data.

    Sequences are created with a call to the create method.

    >>> import sima # doctest: +ELLIPSIS
    ...
    >>> from sima.sequence import Sequence
    >>> Sequence.create('HDF5', 'path.h5', 'tzyxc')


    Attributes
    ----------
    shape : tuple
        (num_frames, num_planes, num_rows, num_columns, num_channels)

    """
    __metaclass__ = ABCMeta

    def __getitem__(self, indices):
        """Create a new Sequence by slicing this Sequence."""
        return _IndexedSequence(self, indices)

    @abstractmethod
    def __iter__(self):
        """Iterate over the frames of the Sequence.

        The yielded structures are numpy arrays of the shape (num_planes,
        num_rows, num_columns, num_channels).
        """
        raise NotImplementedError

    def _get_frame(self, t):
        raise NotImplementedError

    @abstractmethod
    def _todict(self):
        raise NotImplementedError

    @classmethod
    def _from_dict(cls, d, savedir=None):
        """Create a Sequence instance from a dictionary."""
        if savedir is not None:
            _resolve_paths(d, savedir)
        return cls(**d)

    def __len__(self):
        return sum(1 for _ in self)

    @property
    def shape(self):
        return (len(self),) + iter(self).next().shape

    def toarray(self, squeeze=False):
        """Convert to a numpy array.

        Arguments
        ---------
        squeeze : bool

        Returns
        -------
        array : numpy.ndarray
            The pixel values from the dataset as a numpy array
            with the same shape as the Sequence.
        """
        return np.concatenate(np.expand_dims(x, 0) for x in self)

    @classmethod
    def create(cls, fmt, *args, **kwargs):
        if fmt == 'HDF5':
            return _Sequence_HDF5(*args, **kwargs)

    def export(self, filenames, fmt='TIFF16', fill_gaps=False,
               scale_values=False, channel_names=None):
        """Save frames to the indicated filenames.

        This function stores a multipage tiff file for each channel.
        """
        for filename in filenames:
            if dirname(filename):
                sima.misc.mkdir_p(dirname(filename))

        if 'TIFF' in fmt:
            output_files = [TiffFileWriter(fn) for fn in filenames]
        elif fmt == 'HDF5':
            if not h5py_available:
                raise ImportError('h5py >= 2.3.1 required')
            f = h5py.File(filenames, 'w')
            output_array = np.empty((self.num_frames, 1,
                                     self.num_rows,
                                     self.num_columns,
                                     self.num_channels), dtype='uint16')
        else:
            raise('Not Implemented')

        if fill_gaps:
            save_frames = _fill_gaps(iter(self), iter(self))
        else:
            save_frames = iter(self)
        for f_idx, frame in enumerate(save_frames):
            for ch_idx, channel in enumerate(frame):
                if fmt == 'TIFF16':
                    f = output_files[ch_idx]
                    if scale_values:
                        f.write_page(sima.misc.to16bit(channel))
                    else:
                        f.write_page(channel.astype('uint16'))
                elif fmt == 'TIFF8':
                    f = output_files[ch_idx]
                    if scale_values:
                        f.write_page(sima.misc.to8bit(channel))
                    else:
                        f.write_page(channel.astype('uint8'))
                elif fmt == 'HDF5':
                    output_array[f_idx, 0, :, :, ch_idx] = channel
                else:
                    raise ValueError('Unrecognized output format.')

        if 'TIFF' in fmt:
            for f in output_files:
                f.close()
        elif fmt == 'HDF5':
            f.create_dataset(name='imaging', data=output_array)
            for idx, label in enumerate(['t', 'z', 'y', 'x', 'c']):
                f['imaging'].dims[idx].label = label
            if channel_names is not None:
                f['imaging'].attrs['channel_names'] = np.array(channel_names)
            f.close()


class _IndexableSequence(Sequence):

    """Iterable whose underlying structure supports indexing."""
    __metaclass__ = ABCMeta

    def __iter__(self):
        for t in xrange(len(self)):
            yield self._get_frame(t)

    # @abstractmethod
    # def _get_frame(self, t):
    #     """Return frame with index t."""
    #     pass


# class _SequenceMultipageTIFF(_BaseSequence):
#
#     """
#     Iterable for a multi-page TIFF file in which the pages
#     correspond to sequentially acquired image frames.
#
#     Parameters
#     ----------
#     paths : list of str
#         The TIFF filenames, one per channel.
#     clip : tuple of tuple of int, optional
#         The number of rows/columns to clip from each edge
#         in order ((top, bottom), (left, right)).
#
#     Warning
#     -------
#     Moving the TIFF files may make this iterable unusable
#     when the ImagingDataset is reloaded. The TIFF file can
#     only be moved if the ImagingDataset path is also moved
#     such that they retain the same relative position.
#
#     """
#
#     def __init__(self, paths, clip=None):
#         super(MultiPageTIFF, self).__init__(clip)
#         self.path = abspath(path)
#         if not libtiff_available:
#             self.stack = TiffFile(self.path)
#
#     def __len__(self):
# TODO: remove this and just use
#         if libtiff_available:
#             tiff = TIFF.open(self.path, 'r')
#             l = sum(1 for _ in tiff.iter_images())
#             tiff.close()
#             return l
#         else:
#             return len(self.stack.pages)
#
#     def __iter__(self):
#         if libtiff_available:
#             tiff = TIFF.open(self.path, 'r')
#             for frame in tiff.iter_images():
#                 yield frame
#         else:
#             for frame in self.stack.pages:
#                 yield frame.asarray(colormapped=False)
#         if libtiff_available:
#             tiff.close()
#
#     def _todict(self):
#         return {'path': self.path, 'clip': self._clip}


class _Sequence_HDF5(_IndexableSequence):

    """
    Iterable for an HDF5 file containing imaging data.

    Parameters
    ----------
    path : str
        The HDF5 filename, typicaly with .h5 extension.
    dim_order : str
        Specification of the order of the dimensions. This
        string can contain the letters 't', 'x', 'y', 'z',
        and 'c', representing time, column, row, plane,
        and channel, respectively.
        For example, 'tzyxc' indicates that the HDF5 data
        dimensions represent time (t), plane (z), row (y),
        column(x), and channel (c), respectively.
        The string 'tyx' indicates data that data for a single
        imaging plane and single channel has been stored in a
        HDF5 dataset with three dimensions representing time (t),
        column (y), and row (x) respectively.
        Note that SIMA 0.1.x does not support multiple z-planes,
        although these will be supported in future versions.
    group : str, optional
        The HDF5 group containing the imaging data.
        Defaults to using the root group '/'
    key : str, optional
        The key for indexing the the HDF5 dataset containing
        the imaging data. This can be omitted if the HDF5
        group contains only a single key.

    Warning
    -------
    Moving the HDF5 file may make this iterable unusable
    when the ImagingDataset is reloaded. The HDF5 file can
    only be moved if the ImagingDataset path is also moved
    such that they retain the same relative position.

    """

    def __init__(self, path, dim_order, group=None, key=None):
        if not h5py_available:
            raise ImportError('h5py >= 2.3.1 required')
        self.path = abspath(path)
        self._file = h5py.File(path, 'r')
        if group is None:
            group = '/'
        self._group = self._file[group]
        if key is None:
            if len(self._group.keys()) != 1:
                raise ValueError(
                    'key must be provided to resolve ambiguity.')
            key = self._group.keys()[0]
        self._key = key
        self._dataset = self._group[key]
        if len(dim_order) != len(self._dataset.shape):
            raise ValueError(
                'dim_order must have same length as the number of ' +
                'dimensions in the HDF5 dataset.')
        self._T_DIM = dim_order.find('t')
        self._Z_DIM = dim_order.find('z')
        self._Y_DIM = dim_order.find('y')
        self._X_DIM = dim_order.find('x')
        self._C_DIM = dim_order.find('c')
        self._dim_order = dim_order

    def __len__(self):
        return self._dataset.shape[self._T_DIM]
        # indices = self._time_slice.indices(self._dataset.shape[self._T_DIM])
        # return (indices[1] - indices[0] + indices[2] - 1) // indices[2]

    def _get_frame(self, t):
        """Get the frame at time t, but not clipped"""
        slices = [slice(None) for _ in range(len(self._dataset.shape))]
        swapper = [None for _ in range(len(self._dataset.shape))]
        if self._Z_DIM > -1:
            swapper[self._Z_DIM] = 0
        swapper[self._Y_DIM] = 1
        swapper[self._X_DIM] = 2
        if self._C_DIM > -1:
            swapper[self._C_DIM] = 3
        swapper = filter(lambda x: x is not None, swapper)
        slices[self._T_DIM] = t
        frame = self._dataset[tuple(slices)]
        for i in range(frame.ndim):
            idx = np.argmin(swapper[i:]) + i
            if idx != i:
                swapper[i], swapper[idx] = swapper[idx], swapper[i]
                frame.swapaxes(i, idx)
        return frame.astype(float)

    def _todict(self):
        return {
            '__class__': self.__class__,
            'path': abspath(self.path),
            'dim_order': self._dim_order,
            'group': self._group.name,
            'key': self._key,
        }


class _WrapperSequence(Sequence):
    "Abstract class for wrapping a Sequence to modify its functionality"""
    __metaclass__ = ABCMeta

    def __init__(self, base):
        self._base = base

    def __getattr__(self, name):
        try:
            getattr(super(_WrapperSequence, self), name)
        except AttributeError as err:
            if err.args[0] == \
                    "'super' object has no attribute '" + name + "'":
                return getattr(self._base, name)
            else:
                raise err

    def _todict(self):
        raise NotImplementedError

    @classmethod
    def _from_dict(cls, d, savedir=None):
        base_dict = d.pop('base')
        base_class = base_dict.pop('__class__')
        base = base_class._from_dict(base_dict, savedir)
        return cls(base, **d)


class _MotionCorrectedSequence(_WrapperSequence):

    """Wraps any other sequence to apply motion correction.

    Parameters
    ----------
    base : Sequence

    displacements : array
        The _D displacement of each row in the image cycle.
        Shape: (num_rows * num_frames, 2).

    This object has the same attributes and methods as the class it wraps."""
    # TODO: check clipping and output frame size

    def __init__(self, base, displacements, frame_shape):
        super(_MotionCorrectedSequence, self).__init__(base)
        self.displacements = displacements
        self._frame_shape = frame_shape  # (planes, rows, columns)

    def __len__(self):
        return len(self._base)  # Faster to calculate len without aligning

    def __iter__(self):
        for frame, displacement in itertools.izip(self._base,
                                                  self.displacements):
            yield _align_frame(frame, displacement, self._frame_shape)

    def _get_frame(self, t):
        return _align_frame(self._base._get_frame(t).astype(float),
                           self.displacements[t], self._frame_shape)

    def __getitem__(self, indices):
        if len(indices) > 5:
            raise ValueError
        indices = indices if isinstance(indices, tuple) else (indices,)
        times = indices[0]
        if indices[0] not in (None, slice(None)):
            new_indices = (None,) + indices[1:]
            return _MotionCorrectedSequence(
                self._base[times],
                self.displacements[times],
                self._frame_shape
            )[new_indices]
        if len(indices) == 5:
            chans = indices[5]
            return _MotionCorrectedSequence(
                self._base[:, :, :, :, chans],
                self.displacements[:, :, :, chans],
                self._frame_shape
            )[indices[:5]]
        # TODO: similar for planes ???
        return _IndexedSequence(self, indices)

    def _todict(self):
        return {
            '__class__': self.__class__,
            'base': self._base._todict(),
            'displacements': self.displacements,
            'frame_shape': self._frame_shape,
        }


class _InvalidFramesSequence(_WrapperSequence):
    pass


class _IndexedSequence(_WrapperSequence):

    def __init__(self, base, indices):
        super(_IndexedSequence, self).__init__(base)
        self._base_len = len(base)
        self._indices = \
            indices if isinstance(indices, tuple) else (indices,)
        # Reformat integer slices to avoid dimension collapse
        self._indices = tuple(
            slice(i, i+1) if isinstance(i, int) else i
            for i in self._indices)
        self._times = range(self._base_len)[self._indices[0]]
        # TODO: switch to generator/iterator if possible?

    def __iter__(self):
        try:
            for t in self._times:
                yield self._base._get_frame(t)[self._indices[1:]]
        except AttributeError as err:
            if not err.args[0] == \
                    "'super' object has no attribute '_get_frame'":
                raise err
            idx = 0
            for t, frame in enumerate(self._base):
                try:
                    whether_yield = t == self._times[idx]
                except IndexError:
                    raise StopIteration
                if whether_yield:
                    yield frame[self._indices[1:]]
                    idx += 1

    def _get_frame(self, t):
        return self._base._get_frame(self._times[t])[self._indices[1:]]

    def __len__(self):
        return len(range(len(self._base))[self._indices[0]])

    def _todict(self):
        return {
            '__class__': self.__class__,
            'base': self._base._todict(),
            'indices': self._indices
        }

    # def __dir__(self):
    #     """Customize how attributes are reported, e.g. for tab completion.

    #     This may not be necessary if we inherit an abstract class"""
    #     heritage = dir(super(self.__class__, self)) # inherited attributes
    #     return sorted(heritage + self.__class__.__dict__.keys() +
    #                   self.__dict__.keys())


def _fill_gaps(frame_iter1, frame_iter2):
    """Fill missing rows in the corrected images with data from nearby times.

    Parameters
    ----------
    frame_iter1 : iterator of list of array
        The corrected frames (one list entry per channel).
    frame_iter2 : iterator of list of array
        The corrected frames (one list entry per channel).

    Yields
    ------
    list of array
        The corrected and filled frames.
    """
    first_obs = next(frame_iter1)
    for frame in frame_iter1:
        for frame_chan, fobs_chan in zip(frame, first_obs):
            fobs_chan[np.isnan(fobs_chan)] = frame_chan[np.isnan(fobs_chan)]
        if all(np.all(np.isfinite(chan)) for chan in first_obs):
            break
    most_recent = [x * np.nan for x in first_obs]
    for frame in frame_iter2:
        for fr_chan, mr_chan in zip(frame, most_recent):
            mr_chan[np.isfinite(fr_chan)] = fr_chan[np.isfinite(fr_chan)]
        yield [np.nan_to_num(mr_ch) + np.isnan(mr_ch) * fo_ch
               for mr_ch, fo_ch in zip(most_recent, first_obs)]


from scipy.cluster.vq import kmeans2
from itertools import chain


def _detect_artifact(self, channels=None):
    """Detect pixels that have been saturated by an external source

    NOTE: this is written to deal with an artifact specific to our lab.

    Parameters
    ----------
    channels : list of int
        The channels in which artifact light is to be detected.

    Returns
    -------
    dict of (int, array)
        Channel indices index boolean arrays indicating whether the rows
        have valid (i.e. not saturated) data.
        Array shape: (num_cycles, num_rows*num_timepoints).
    """
    channels = [] if channels is None else channels
    ret = {}
    for channel in channels:
        row_intensities = []
        for frame in chain(*self):
            im = frame[channel].astype('float')
            row_intensities.append(im.mean(axis=1))
        row_intensities = np.array(row_intensities)
        for i in range(row_intensities.shape[1]):
            row_intensities[:, i] += -row_intensities[:, i].mean() + \
                row_intensities.mean()  # remove periodic component
        row_intensities = row_intensities.reshape(-1)
        # Separate row means into 2 clusters
        [centroid, labels] = kmeans2(row_intensities, 2)
        # only discard rows if clusters are substantially separated
        if max(centroid) / min(centroid) > 3 and \
                max(centroid) - min(centroid) > 2 * np.sqrt(sum([np.var(
                row_intensities[np.equal(labels, i)]) for i in [0, 1]])):
            # row intensities in the lower cluster are valid
            valid_rows = np.equal(labels, np.argmin(centroid))
            # also exclude rows prior to those in the higher cluster
            valid_rows[:-1] *= valid_rows[1:].copy()
            # also exclude rows following those in the higher cluster
            valid_rows[1:] *= valid_rows[:-1].copy()
        else:
            valid_rows = np.ones(labels.shape).astype('bool')
        # Reshape back to a list of arrays, one per cycle
        row_start = 0
        valid_rows_by_cycle = []
        for cycle in self:
            valid_rows_by_cycle.append(
                valid_rows[row_start:
                           row_start + cycle.num_frames * cycle.num_rows])
            row_start += cycle.num_frames * cycle.num_rows
        ret[channel] = valid_rows_by_cycle
    return ret


def _resolve_paths(d, savedir):
    paths = set()
    try:
        relp = d.pop('_relpath')
    except KeyError:
        pass
    else:
        paths.add(normcase(abspath(normpath(join(savedir, relp)))))
    try:
        paths.add(normcase(abspath(normpath(d.pop('_abspath')))))
    except KeyError:
        pass
    if len(paths):
        paths = filter(isfile, paths)
        if len(paths) != 1:
            testfile = paths.pop()
            if not all(samefile(testfile, p) for p in paths):
                raise Exception(
                    'Files have been moved. The path '
                    'cannot be unambiguously resolved.'
                )
        d['path'] = paths.pop()