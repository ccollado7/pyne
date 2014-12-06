#! /usr/bin/env python
"""This file generates a static C++ decayer function for use with PyNE.
It is suppossed to be fast.
"""
import io
import warnings
from argparse import ArgumentParser, Namespace

import numpy as np
warnings.simplefilter('ignore', RuntimeWarning)
import tables as tb
import jinja2

from pyne.utils import QAWarning, toggle_warnings
warnings.simplefilter('ignore', QAWarning)
toggle_warnings()
from pyne import nuc_data
from pyne import data
from pyne import rxname
from pyne import nucname
from pyne.data import branch_ratio, half_life, decay_const, decay_children, \
    decay_data_children, fpyield
from pyne.material import Material

def _branch_ratio(f):
    special_cases = {
        (451040000, 461040000): 0.9955, 
        (451040000, 441040000): 0.0045, 
        (521270000, 531270000): 1.0,
        (471100001, 471100000): 1.0 - 0.9867, 
        (491190001, 491190000): 1.0 - 0.956,
        (511260001, 511260000): 0.14,
        (320770001, 320770000): 0.19, 
        (360850001, 360850000): 1.0 - 0.788, 
        (711770001, 711770000): 0.217, 
        (461110001, 461110000): 0.73,
        (842110001, 842110000): 0.0002,
        (521290001, 531290000): 0.63,
        }
    def br(x, y):
        if (x, y) in special_cases:
            return special_cases[x, y]
        return f(x, y)
    return br
branch_ratio = _branch_ratio(branch_ratio)


def _decay_children(f):
    special_cases = {
        451040000: set([441040000, 461040000]),
        521270000: set([531270000]),
        }
    def dc(x):
        if x in special_cases:
            return special_cases[x]
        return f(x)
    return dc
decay_children = _decay_children(decay_children)
            

ENV = jinja2.Environment(undefined=jinja2.StrictUndefined)

autogenwarn = """
// !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
// !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
//              WARNING
// This file has been auto generated
// Do not modify directly. You have
// been warned. This is that warning
// !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
// !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
""".strip()

HEADER = ENV.from_string("""
#ifndef PYNE_GEUP5PGEJBFGNHGI36TRBB4WGM
#define PYNE_GEUP5PGEJBFGNHGI36TRBB4WGM

{{ autogenwarn }}

#include <map>
//#include <cmath>

#include "data.h"
#include "nucname.h"

namespace pyne {
namespace decayers {

extern const int all_nucs[{{ nucs|length }}];

std::map<int, double> decay(std::map<int, double> comp, double t);

}  // namespace decayers
}  // namespace pyne

#endif  // PYNE_GEUP5PGEJBFGNHGI36TRBB4WGM
""".strip())

SOURCE = ENV.from_string("""
{{ autogenwarn }}

#include "decay.h"

namespace pyne {
namespace decayers {

{{ funcs }}

std::map<int, double> decay(std::map<int, double> comp, double t) {
  // setup
  using std::map;
  int nuc;
  int i = 0;
  double out [{{ nucs|length }}] = {};  // init to zero
  map<int, double> outcomp;
  
  // body
  map<int, double>::const_iterator it = comp.begin();
  for (; it != comp.end(); ++it) {
    switch (nucname::znum(it->first)) {
      {{ cases|indent(6) }}
      default:
        outcomp.insert(*it);
        break;
    }
  }
  
  // cleanup
  for (i = 0; i < {{ nucs|length }}; ++i)
    if (out[i] > 0.0)
      outcomp[all_nucs[i]] = out[i];
  return outcomp;
}

const int all_nucs [{{ nucs|length }}] = {
{{ nucs | join(", ") | wordwrap(width=78, break_long_words=False) | indent(2, True) }}
};

}  // namespace decayers
}  // namespace pyne
""".strip())


ELEM_FUNC = ENV.from_string("""
void decay_{{ elem|lower }}(double &t, std::map<int, double>::const_iterator &it, std::map<int, double> &outcomp, double (&out)[{{ nucs|length }}]) {
  //using std::exp2;
  switch (it->first) {
    {{ cases|indent(4) }}
    default:
      outcomp.insert(*it);
      break;
  }
}
""".strip())


# Some strings that need not be redefined
BREAK = '  break;'
CHAIN_STMT = '  out[{0}] += {1};'
CHAIN_EXPR = '(it->second) * ({0})'
EXP_EXPR = 'exp2({a:e}*t)'
KEXP_EXPR = '{k:e}*' + EXP_EXPR

def genfiles(nucs):
    ctx = Namespace(
        nucs=nucs,
        autogenwarn=autogenwarn,
        )
    ctx.cases = gencases(nucs)
    ctx.funcs = genelemfuncs(nucs)
    hdr = HEADER.render(ctx.__dict__)
    src = SOURCE.render(ctx.__dict__)
    return hdr, src


def genchains(chains):
    chain = chains[-1]
    children = decay_children(chain[-1])
    children = {c for c in children if 0.0 == fpyield(chain[-1], c)}
    #children = {c for c in children if 1e-8 < branch_ratio(chain[-1], c)}
    for child in children:
        chains.append(chain + (child,))
        chains = genchains(chains)
    return chains

def k_a(chain):
    # gather data
    hl = np.array([half_life(n) for n in chain])
    a = -1.0 / hl
    dc = np.array(list(map(decay_const, chain)))
    if np.isnan(dc).any():
        # NaNs are bad, mmmkay.  Nones mean we should skip
        return None, None  
    #ends_stable = (dc[-1] == 0.0)  # check if last nuclide is a stable species
    ends_stable = (dc[-1] < 1e-16)  # check if last nuclide is a stable species
    # compute cij -> ci in prep for k
    cij = dc[:, np.newaxis] / (dc[:, np.newaxis] - dc)
    if ends_stable:
        cij[-1] = -1.0 / dc  # adjustment for stable end nuclide
    mask = np.ones(len(chain), dtype=bool)
    cij[mask, mask] = 1.0  # identity is ignored, set to unity
    ci = cij.prod(axis=0)
    # compute k
    if ends_stable:
        k = dc * ci
        k[-1] = 1.0
    else:
        k = (dc / dc[-1]) * ci
    if np.isinf(k).any():
        # if this happens then something wen very wrong, skip
        return None, None 
    # compute and apply branch ratios
    gamma = np.prod([branch_ratio(p, c) for p, c in zip(chain[:-1], chain[1:])])
    if gamma == 0.0:
        return None, None
    k *= gamma
    # no filter
    #return k, a
    # k filter
    #kfrac = np.abs(k) / np.sum(np.abs(k))
    #mask = (kfrac > 1e-8)
    #return k[mask], a[mask]
    # half-life  filter, makes compiling faster by pre-ignoring negligible species 
    # in this chain. They'll still be picked up in their own chains.
    if ends_stable:
        mask = (hl[:-1] / hl[:-1].sum()) > 1e-8
        mask = np.append(mask, True)
    else:
        mask = (hl / hl.sum()) > 1e-8
    if mask.sum() < 2:
        mask = np.ones(len(chain), dtype=bool)
    return k[mask], a[mask]


def kexpexpr(k, a):
    if k == 1.0:
        return EXP_EXPR.format(a=a)
    else:
        return KEXP_EXPR.format(k=k, a=a)


def chainexpr(chain):
    child = chain[-1]
    dc_child = decay_const(child)
    if len(chain) == 1:
        a = -1.0 / half_life(child)
        terms = EXP_EXPR.format(a=a)
    else:
        k, a = k_a(chain)
        if k is None:
            return None
        terms = [] 
        for k_i, a_i in zip(k, a):
            if k_i == 1.0 and a_i == 0.0:
                # a slight optimization 
                term = '1.0'
            else:
                term = kexpexpr(k_i, a_i) 
            terms.append(term)
        terms = ' + '.join(terms)
    return CHAIN_EXPR.format(terms)


def gencase(nuc, idx):
    case = ['case {0}:'.format(nuc)]
    dc = decay_const(nuc)
    if dc == 0.0:
        # stable nuclide
        case.append(CHAIN_STMT.format(idx[nuc], 'it->second'))
    else:
        chains = genchains([(nuc,)])
        print(len(chains), len(set(chains)), nuc)
        for c in chains:
            if c[-1] not in idx:
                continue
            cexpr = chainexpr(c)
            if cexpr is None:
                continue
            case.append(CHAIN_STMT.format(idx[c[-1]], cexpr))
    case.append(BREAK)
    return case


def elems(nucs):
    return sorted(set(map(nucname.znum, nucs)))


def gencases(nucs):
    switches = []
    for i in elems(nucs):
        c = ['case {0}:'.format(i), 
             '  decay_{0}(t, it, outcomp, out);'.format(nucname.name(i).lower()), 
             '  break;']
        switches.append('\n'.join(c))
    return '\n'.join(switches)


def genelemfuncs(nucs):
    idx = dict(zip(nucs, range(len(nucs))))
    cases = {i: [] for i in elems(nucs)}
    for nuc in nucs:
        cases[nucname.znum(nuc)] += gencase(nuc, idx)
    funcs = []
    for i, kases in cases.items():
        ctx = dict(nucs=nucs, elem=nucname.name(i), cases='\n'.join(kases))
        funcs.append(ELEM_FUNC.render(ctx))
    return "\n\n".join(funcs)


def load_default_nucs():
    with tb.open_file(nuc_data) as f:
        ll = f.root.decay.level_list
        stable = ll.read_where('(nuc_id%10000 == 0) & (nuc_id != 0)')
        metastable = ll.read_where('metastable > 0')
    nucs = set(int(nuc) for nuc in stable['nuc_id']) 
    nucs |= set(nucname.state_id_to_id(int(nuc)) for nuc in metastable['nuc_id']) 
    nucs = sorted(nuc for nuc in nucs if not np.isnan(decay_const(nuc)))
    return nucs


def main():
    parser = ArgumentParser('decay-gen')
    parser.add_argument('--hdr', default='decay.h', help='The header file name.')
    parser.add_argument('--src', default='decay.cpp', help='The source file name.')
    parser.add_argument('--nucs', nargs='+', default=None, 
                        help='Nuclides to generate for.')
    ns = parser.parse_args()
    nucs = load_default_nucs() if ns.nucs is None else list(map(nucname.id, ns.nucs))
    hdr, src = genfiles(nucs)
    with io.open(ns.hdr, 'w') as f:
        f.write(hdr)
    with io.open(ns.src, 'w') as f:
        f.write(src)


if __name__ == '__main__':
    main()
