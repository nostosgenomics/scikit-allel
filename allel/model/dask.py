# -*- coding: utf-8 -*-
"""This module provides alternative implementations of array and table
classes defined in the :mod:`allel.model.ndarray` module, using
`dask.array <http://dask.pydata.org/en/latest/array.html>`_ as the
computational engine.

Dask uses blocked algorithms and task scheduling to break up work into
smaller pieces, allowing computation over very large datasets. It also uses
lazy evaluation, meaning that multiple operations can be chained together
into a task graph, reducing total memory requirements for intermediate
results, and only the tasks required to generate the requested
part of the final data set will be executed.

This module is experimental, if you find a bug please `raise an issue on GitHub
<https://github.com/cggh/scikit-allel/issues/new>`_.

Currently this module requires a specific branch of Dask to be installed::

    $ pip install git+https://github.com/mrocklin/dask.git@drop-new-axes

"""
from __future__ import absolute_import, print_function, division


import numpy as np
import dask.array as da


from allel.model.ndarray import GenotypeArray, HaplotypeArray, \
    AlleleCountsArray


def get_chunks(data, chunks=None):
    """Try to guess a reasonable chunk shape to use for block-wise
    algorithms operating over `data`."""

    if chunks is None:

        if hasattr(data, 'chunklen') and hasattr(data, 'shape'):
            # bcolz carray, chunk first dimension only
            return (data.chunklen,) + data.shape[1:]

        elif hasattr(data, 'chunks') and hasattr(data, 'shape') and \
                len(data.chunks) == len(data.shape):
            # h5py dataset
            return data.chunks

        else:
            # fall back to something simple, ~1Mb chunks of first dimension
            row = np.asarray(data[0])
            chunklen = max(1, (2**20) // row.nbytes)
            if row.shape:
                chunks = (chunklen,) + row.shape
            else:
                chunks = (chunklen,)
            return chunks

    else:

        return chunks


def ensure_array_like(data):
    if not hasattr(data, 'shape') or not hasattr(data, 'dtype'):
        a = np.asarray(data)
        if len(a.shape) == 0:
            raise ValueError('not array-like')
        return a
    else:
        return data


def ensure_dask_array(data, chunks=None):
    if isinstance(data, da.Array):
        if chunks:
            data = data.rechunk(chunks)
        return data
    else:
        data = ensure_array_like(data)
        chunks = get_chunks(data, chunks)
        return da.from_array(data, chunks=chunks)


def view_subclass(darr, cls):
    """View a dask Array as an instance of a dask Array sub-class."""
    return cls(darr.dask, name=darr.name, chunks=darr.chunks,
               dtype=darr.dtype, shape=darr.shape)


# noinspection PyAbstractClass
class DaskArrayAug(da.Array):

    @classmethod
    def from_array(cls, x, chunks=None, name=None, lock=False):
        # override this as a class method to allow sub-classes to return
        # instances of themselves

        # ensure array-like
        x = ensure_array_like(x)
        if hasattr(cls, 'check_input_data'):
            cls.check_input_data(x)

        # determine chunks, guessing something reasonable if user does not
        # specify
        chunks = get_chunks(x, chunks)

        # create vanilla dask array
        darr = da.from_array(x, chunks=chunks, name=name, lock=lock)

        # view as sub-class
        return view_subclass(darr, cls)

    def __repr__(self):
        r = super(DaskArrayAug, self).__repr__()
        return '%s%s' % (type(self).__name__, r[10:])

    def compress(self, condition, axis=0):
        if axis == 0:
            out = self[condition]
        elif axis == 1:
            out = self[:, condition]
        else:
            raise NotImplementedError('axis not implemented')
        return view_subclass(out, type(self))

    def take(self, indices, axis=0):
        if axis == 0:
            out = self[indices]
        elif axis == 1:
            out = self[:, indices]
        else:
            raise NotImplementedError('axis not implemented')
        return view_subclass(out, type(self))

    def subset(self, sel0, sel1):
        out = self[sel0][:, sel1]
        return view_subclass(out, type(self))

    def hstack(self, *others, **kwargs):
        others = tuple(ensure_dask_array(d) for d in others)
        tup = (self,) + others
        out = da.concatenate(tup, axis=1)
        return view_subclass(out, type(self))

    def vstack(self, *others, **kwargs):
        others = tuple(ensure_dask_array(d) for d in others)
        tup = (self,) + others
        out = da.concatenate(tup, axis=0)
        return view_subclass(out, type(self))


# noinspection PyAbstractClass
class GenotypeDaskArray(DaskArrayAug):
    """TODO"""

    def __init__(self, *args, **kwargs):
        super(GenotypeDaskArray, self).__init__(*args, **kwargs)
        self._mask = None

    @staticmethod
    def check_input_data(x):
        if len(x.shape) != 3:
            raise ValueError('expected 3 dimensions')
        # don't check dtype now as it forces compute()

    def __getitem__(self, *args):
        out = super(GenotypeDaskArray, self).__getitem__(*args)
        if hasattr(out, 'shape') \
                and len(self.shape) == len(out.shape) \
                and self.shape[2] == out.shape[2]:
            # dimensionality and ploidy preserved
            out = view_subclass(out, GenotypeDaskArray)
            if self.mask is not None:
                # attempt to slice mask too
                m = self.mask.__getitem__(*args)
                out.mask = m
        return out

    def compute(self, **kwargs):
        a = super(GenotypeDaskArray, self).compute(**kwargs)
        g = GenotypeArray(a)
        if self.mask:
            m = self.mask.compute(**kwargs)
            g.mask = m
        return g

    def _repr_html_(self):
        return self[:6].compute().to_html_str(caption=repr(self))

    @property
    def n_variants(self):
        return self.shape[0]

    @property
    def n_samples(self):
        return self.shape[1]

    @property
    def ploidy(self):
        return self.shape[2]

    @property
    def n_calls(self):
        return self.shape[0] * self.shape[1]

    @property
    def n_allele_calls(self):
        return self.shape[0] * self.shape[1] * self.shape[2]

    @property
    def mask(self):
        return self._mask

    @mask.setter
    def mask(self, mask):

        # ensure dask array
        mask = ensure_dask_array(mask, chunks=self.chunks[:2])

        # check shape
        if mask.shape != self.shape[:2]:
            raise ValueError('mask has incorrect shape')

        # store
        self._mask = mask

    def _method(self, method_name, chunks=None, drop_dims=None, **kwargs):
        if chunks is None:
            # no shape change
            chunks = self.chunks

        if self.mask is None:
            # simple case, no mask
            def f(block):
                g = GenotypeArray(block)
                method = getattr(g, method_name)
                return method(**kwargs)
            out = self.map_blocks(f, chunks=chunks, drop_dims=drop_dims)
            
        else:
            # map with mask
            def f(block, bmask):
                g = GenotypeArray(block)
                g.mask = bmask[:, :, 0]
                method = getattr(g, method_name)
                return method(**kwargs)
            m = self.mask[:, :, None]
            out = da.map_blocks(f, self, m, chunks=chunks, 
                                drop_dims=drop_dims)
            
        return out

    def _method_drop_dim2(self, method_name, **kwargs):
        chunks = self.chunks[:2]
        return self._method(method_name, chunks=chunks, drop_dims=2, **kwargs)

    def fill_masked(self, value=-1):
        out = self._method('fill_masked', value=value)
        return view_subclass(out, GenotypeDaskArray)

    def is_called(self):
        return self._method_drop_dim2('is_called')

    def is_missing(self):
        return self._method_drop_dim2('is_missing')

    def is_hom(self, allele=None):
        return self._method_drop_dim2('is_hom', allele=allele)

    def is_hom_ref(self):
        return self._method_drop_dim2('is_hom_ref')

    def is_hom_alt(self):
        return self._method_drop_dim2('is_hom_alt')

    def is_het(self, allele=None):
        return self._method_drop_dim2('is_het', allele=allele)

    def is_call(self, call):
        return self._method_drop_dim2('is_call', call=call)

    def _count(self, method_name, axis, **kwargs):
        method = getattr(self, method_name)
        out = method(**kwargs).sum(axis=axis)
        if axis is None:
            # result is scalar, might as well compute now (also helps tests)
            return out.compute()[()]
        else:
            return out

    def count_called(self, axis=None):
        return self._count('is_called', axis)

    def count_missing(self, axis=None):
        return self._count('is_missing', axis)

    def count_hom(self, allele=None, axis=None):
        return self._count('is_hom', axis, allele=allele)

    def count_hom_ref(self, axis=None):
        return self._count('is_hom_ref', axis)

    def count_hom_alt(self, axis=None):
        return self._count('is_hom_alt', axis)

    def count_het(self, allele=None, axis=None):
        return self._count('is_het', axis, allele=allele)

    def count_call(self, call, axis=None):
        return self._count('is_call', axis, call=call)

    def count_alleles(self, max_allele=None, subpop=None):

        # if max_allele not specified, count all alleles
        if max_allele is None:
            max_allele = self.max().compute()[()]

        # deal with subpop
        if subpop:
            gd = self.take(subpop, axis=1)
        else:
            gd = self

        # determine output chunks - preserve dim0; change dim1, dim2
        chunks = (gd.chunks[0], (1,)*len(gd.chunks[1]), (max_allele+1,))

        if self.mask is None:

            # simple case, no mask
            def f(block):
                gb = GenotypeArray(block)
                return gb.count_alleles(max_allele=max_allele)[:, None, :]

            # map blocks and reduce
            out = gd.map_blocks(f, chunks=chunks).sum(axis=1)

        else:

            # map with mask
            def f(block, bmask):
                g = GenotypeArray(block)
                g.mask = bmask[:, :, 0]
                return g.count_alleles(max_allele=max_allele)[:, None, :]

            md = self.mask[:, :, None]
            out = da.map_blocks(f, gd, md, chunks=chunks).sum(axis=1)

        return view_subclass(out, AlleleCountsDaskArray)

    def count_alleles_subpops(self, subpops, max_allele=None):

        # if max_allele not specified, count all alleles
        if max_allele is None:
            max_allele = self.max().compute()[()]

        return {k: self.count_alleles(max_allele=max_allele, subpop=v)
                for k, v in subpops.items()}

    def to_packed(self, boundscheck=True):
        return self._method_drop_dim2('to_packed', boundscheck=boundscheck)

    @staticmethod
    def from_packed(packed, chunks=None):
        def f(block):
            return GenotypeArray.from_packed(block)
        packed = ensure_dask_array(packed, chunks)
        chunks = (packed.chunks[0], packed.chunks[1], (2,))
        out = da.map_blocks(f, packed, chunks=chunks, new_dims=2)
        return view_subclass(out, GenotypeDaskArray)

    def map_alleles(self, mapping, **kwargs):

        def f(block, bmapping):
            g = GenotypeArray(block)
            m = bmapping[:, 0, :]
            return g.map_alleles(m)

        # obtain dask array
        mapping = da.from_array(mapping, chunks=(self.chunks[0], None))

        # map blocks
        out = da.map_blocks(f, self, mapping[:, None, :],
                            chunks=self.chunks)
        return view_subclass(out, GenotypeDaskArray)

    def to_allele_counts(self, alleles=None):

        # determine alleles to count
        if alleles is None:
            m = self.max().compute()[()]
            alleles = list(range(m+1))

        chunks = (self.chunks[0], self.chunks[1], (len(alleles),))
        return self._method('to_allele_counts', chunks=chunks, alleles=alleles)

    def to_gt(self, phased=False, max_allele=None):
        return self._method_drop_dim2('to_gt', phased=phased,
                                      max_allele=max_allele)

    def to_haplotypes(self):
        out = self.reshape(self.shape[0], -1)
        return view_subclass(out, HaplotypeDaskArray)

    def to_n_ref(self, fill=0, dtype='i1'):
        return self._method_drop_dim2('to_n_ref', fill=fill, dtype=dtype)

    def to_n_alt(self, fill=0, dtype='i1'):
        return self._method_drop_dim2('to_n_alt', fill=fill, dtype=dtype)


# noinspection PyAbstractClass
class HaplotypeDaskArray(DaskArrayAug):

    @staticmethod
    def check_input_data(x):
        if len(x.shape) != 2:
            raise ValueError('expected 2 dimensions')
        # don't check dtype now as it forces compute()

    def __getitem__(self, *args):
        out = super(HaplotypeDaskArray, self).__getitem__(*args)
        if hasattr(out, 'shape') and len(self.shape) == len(out.shape):
            # dimensionality preserved
            out = view_subclass(out, HaplotypeDaskArray)
        return out

    def compute(self, **kwargs):
        a = super(HaplotypeDaskArray, self).compute(**kwargs)
        h = HaplotypeArray(a)
        return h

    def _repr_html_(self):
        return self[:6].compute().to_html_str(caption=repr(self))

    @property
    def n_variants(self):
        return self.shape[0]

    @property
    def n_haplotypes(self):
        return self.shape[1]

    def to_genotypes(self, ploidy=2):

        # check ploidy is compatible
        if (self.n_haplotypes % ploidy) > 0:
            raise ValueError('incompatible ploidy')

        # mapper function
        def f(block):
            h = HaplotypeArray(block)
            return h.to_genotypes(ploidy)

        # rechunk across all columns to ensure chunk boundaries don't break
        # individuals
        hd = self.rechunk(chunks={1: self.n_haplotypes})

        # determine output chunks
        chunks = (hd.chunks[0], hd.chunks[1], (ploidy,))

        # map blocks
        out = hd.map_blocks(f, chunks=chunks, new_dims=2)
        return view_subclass(out, GenotypeDaskArray)

    def is_called(self):
        return self >= 0

    def is_missing(self):
        return self < 0

    def is_ref(self):
        return self == 0

    def is_alt(self):
        return self > 0

    def is_call(self, allele):
        return self == allele

    def count_called(self, axis=None):
        return self.is_called().sum(axis=axis)

    def count_missing(self, axis=None):
        return self.is_missing().sum(axis=axis)

    def count_ref(self, axis=None):
        return self.is_ref().sum(axis=axis)

    def count_alt(self, axis=None):
        return self.is_alt().sum(axis=axis)

    def count_call(self, allele, axis=None):
        return self.is_call(allele).sum(axis=axis)

    def count_alleles(self, max_allele=None, subpop=None):

        # if max_allele not specified, count all alleles
        if max_allele is None:
            max_allele = self.max().compute()[()]

        # deal with subpop
        if subpop:
            hd = self.take(subpop, axis=1)
        else:
            hd = self

        # determine output chunks - preserve dim0, change dim1, new dim2
        chunks = (hd.chunks[0], (1,)*len(hd.chunks[1]), (max_allele+1,))

        # mapper function
        def f(block):
            h = HaplotypeArray(block)
            return h.count_alleles(max_allele=max_allele)[:, None, :]

        # map blocks and reduce
        out = hd.map_blocks(f, chunks=chunks, new_dims=2).sum(axis=1)
        return view_subclass(out, AlleleCountsDaskArray)

    def count_alleles_subpops(self, subpops, max_allele=None):

        # if max_allele not specified, count all alleles
        if max_allele is None:
            max_allele = self.max().compute()[()]

        return {k: self.count_alleles(max_allele=max_allele, subpop=v)
                for k, v in subpops.items()}

    def map_alleles(self, mapping, **kwargs):

        def f(block, bmapping):
            h = HaplotypeArray(block)
            return h.map_alleles(bmapping)

        # obtain dask array
        mapping = da.from_array(mapping, chunks=(self.chunks[0], None))

        # map blocks
        out = da.map_blocks(f, self, mapping,
                            chunks=self.chunks)
        return view_subclass(out, HaplotypeDaskArray)


# noinspection PyAbstractClass
class AlleleCountsDaskArray(DaskArrayAug):

    @staticmethod
    def check_input_data(x):
        if len(x.shape) != 2:
            raise ValueError('expected 2 dimensions')
        # don't check dtype now as it forces compute()

    def __getitem__(self, *args):
        out = super(AlleleCountsDaskArray, self).__getitem__(*args)
        if hasattr(out, 'shape') and len(self.shape) == len(out.shape) \
                and self.shape[1] == out.shape[1]:
            # dimensionality and allele indices preserved
            out = view_subclass(out, AlleleCountsDaskArray)
        return out

    def compute(self, **kwargs):
        a = super(AlleleCountsDaskArray, self).compute(**kwargs)
        h = AlleleCountsArray(a)
        return h

    def _repr_html_(self):
        return self[:6].compute().to_html_str(caption=repr(self))

    @property
    def n_variants(self):
        return self.shape[0]

    @property
    def n_alleles(self):
        return self.shape[1]

    def _method(self, method_name, chunks=None, drop_dims=None, **kwargs):
        if chunks is None:
            # no shape change
            chunks = self.chunks

        def f(block):
            ac = AlleleCountsArray(block)
            method = getattr(ac, method_name)
            return method(**kwargs)
        out = self.map_blocks(f, chunks=chunks, drop_dims=drop_dims)
                        
        return out

    def _method_drop_dim1(self, method_name, **kwargs):
        chunks = self.chunks[:1]
        return self._method(method_name, chunks=chunks, drop_dims=1, **kwargs)
    
    def to_frequencies(self, fill=np.nan):
        return self._method('to_frequencies', chunks=self.chunks, fill=fill)

    def allelism(self):
        return self._method_drop_dim1('allelism')

    def max_allele(self):
        return self._method_drop_dim1('max_allele')

    def is_variant(self):
        return self._method_drop_dim1('is_variant')

    def is_non_variant(self):
        return self._method_drop_dim1('is_non_variant')

    def is_segregating(self):
        return self._method_drop_dim1('is_segregating')

    def is_non_segregating(self, allele=None):
        return self._method_drop_dim1('is_non_segregating', allele=allele)

    def is_singleton(self, allele=1):
        return self._method_drop_dim1('is_singleton', allele=allele)

    def is_doubleton(self, allele=1):
        return self._method_drop_dim1('is_doubleton', allele=allele)

    def _count(self, method_name, **kwargs):
        method = getattr(self, method_name)
        # result is scalar, might as well compute now (also helps tests)
        return method(**kwargs).sum().compute()[()]
        
    def count_variant(self):
        return self._count('is_variant')

    def count_non_variant(self):
        return self._count('is_non_variant')

    def count_segregating(self):
        return self._count('is_segregating')

    def count_non_segregating(self, allele=None):
        return self._count('is_non_segregating', allele=allele)

    def count_singleton(self, allele=1):
        return self._count('is_singleton', allele=allele)

    def count_doubleton(self, allele=1):
        return self._count('is_doubleton', allele=allele)

    def map_alleles(self, mapping):

        def f(block, bmapping):
            ac = AlleleCountsArray(block)
            return ac.map_alleles(bmapping)

        # obtain dask array
        mapping = da.from_array(mapping, chunks=(self.chunks[0], None))

        # map blocks
        out = da.map_blocks(f, self, mapping, chunks=self.chunks)
        return AlleleCountsArray(out)
