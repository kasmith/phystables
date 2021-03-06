#################################
#
#
# For making and storing a compressed set of potential paths at every moment in time
#
#
#################################

from __future__ import division, print_function
import numpy as np
import pickle
from .constants import *
from .trials import *
from .tables import SimpleTable, NoisyTable
from multiprocessing import Pool, cpu_count
from OptimTools import async_map, async_apply
import random
import copy
import os

# Unsafe translation of a number between 0 and 256^2-1 to a two character
def to_bin(i):
    x = int(np.floor(i / 256))
    y = i % 256
    return chr(x)+chr(y)

def from_bin(s):
    return ord(s[0])*256 + ord(s[1])

def make_file_name(trnm,kapv,kapb,kapm,perr,nsims,path = '.'):
    kv = str(np.round(kapv))
    kb = str(np.round(kapb))
    km = str(np.round(kapm))
    pe = str(np.round(perr))
    pthname = kv + '_' + kb + '_' + km + '_' + pe + '_' + str(nsims)
    return os.path.join(path,pthname,trnm+'.pmo')

def load_paths(trial,kapv,kapb,kapm,perr,nsims=200,path = '.',verbose=False):
    trpth = make_file_name(trial.name,kapv,kapb,kapm,perr,nsims,path)
    if not os.path.exists(trpth):
        print ('Nothing existing for trial ' + trial.name +'; making now')
        print ('Saving into: ' + trpth)
        trdir = os.path.dirname(trpth)
        if not os.path.exists(trdir): os.makedirs(trdir)
        pm = PathMaker(trial,kapv,kapb,kapm,perr,nsims,verbose=verbose)
        pm.save(trpth)
    else:
        pm = load_path_maker(trpth)
    return pm

def _sample_w_rep(dat, n):
    return [dat[random.randint(0,len(dat)-1)] for _ in xrange(n)]

class Path(object):
    def __init__(self,tab,kv,kb,km,pe,tl,ts,enforcegoal = True,verbose = False, allow_timeout=False,
                 constrained_bounce=False, constrained_move=False):
        ntab = make_noisy(tab,kv,kb,km,pe, constrained_bounce=constrained_bounce, constrained_move=constrained_move)
        nbads = 0
        self.o, self.p, self.b = ntab.simulate(tl,ts,True,True)
        # Make sure that we get a valid goal
        while ((self.o is TIMEUP and not allow_timeout) or self.o is OUTOFBOUNDS or \
                           ntab.balls.bounces > 255) and enforcegoal:
            del ntab
            ntab = makeNoisy(tab,kv,kb,km,pe, constrained_bounce=constrained_bounce, constrained_move=constrained_move)
            nbads += 1
            self.o, self.p, self.b = ntab.simulate(tl,ts,True,True)
        self.maxtime = len(self.p)*ts
        # Test for compression possibilities
        maxpt = max(map(max,self.p))
        minpt = min(map(min,self.p))
        self.len = len(self.p)
        if maxpt > 256*256-1 or minpt < 0: raise RuntimeError('Path out of bounds - not compressible')
        if max(self.b) > 255: raise RuntimeError('Too many bounces to compress')
        self.maxbounce = max(self.b)
        self.ts = ts
        self.tl = tl
        self.initt = tab.tm
        del ntab
        if verbose:
            print ("Done with path; ", tab.tm, "; redos:",nbads)

    def compress(self):
        if self.p is None:
            raise RuntimeError('Already compressed!')
        st = ""
        for p in self.p: st += to_bin(p[0]) + to_bin(p[1])
        self.comp = st
        self.p = None
        bst = ""
        for b in self.b: bst += chr(b)
        self.compb = bst
        self.b = None

    def decompress(self):
        if self.comp is None:
            raise RuntimeError('Already uncompressed')
        self.p = []
        i = 0
        while i < len(self.comp):
            x = from_bin(self.comp[i:(i+2)])
            y = from_bin(self.comp[(i+2):(i+4)])
            self.p.append( (x,y) )
            i += 4
        if len(self.p) != self.len:
            raise RuntimeError('Length mismatch - decompression error!')
        self.comp = None
        self.b = []
        for i in range(len(self.compb)):
            self.b.append(ord(self.compb[i]))
        if len(self.b) != self.len:
            raise RuntimeError('Length mismatch (bounce) - decompression error!')
        self.compb = None

    def getpos(self,t):
        if self.p is None:
            self.decompress()
        used_t = t - self.initt
        if used_t > self.tl:
            return None
        i = int(used_t/self.ts)
        if i >= len(self.p):
            return self.p[-1]
        return self.p[i]

    def getbounce(self,t):
        if self.p is None:
            self.decompress()
        used_t = t - self.initt
        if used_t > self.tl:
            return None
        i = int(used_t/self.ts)
        if i >= len(self.b):
            return self.maxbounce
        return self.b[i]


class PathMaker(object):

    def __init__(self,trial, kapv = KAPV_DEF, kapb = KAPB_DEF, kapm = KAPM_DEF, perr = PERR_DEF, npaths = 100, \
                 pathdist = .1, timelen = 60., timeres = 0.05, cpus = cpu_count(),verbose = False, allow_timeout=False,
                 constrained_bounce=False, constrained_move=False):
        self.trial = trial
        self.npaths = npaths
        self.kv = kapv
        self.kb = kapb
        self.km = kapm
        self.pe = perr
        self.time = timelen
        self.res = timeres
        self.pdist = pathdist
        self.ncpu = cpus
        self.compressed = False
        self.timeout = allow_timeout
        self.cons_bounce = constrained_bounce
        self.cons_move = constrained_move

        self.makePaths(verbose)

    def make_paths(self,verbose=False):
        self.paths = dict()

        # Get the runtime of the table
        tab = self.trial.makeTable()
        r = None
        while r is None: r = tab.step(self.pdist)
        maxtm = tab.tm
        self.maxtm = maxtm
        ntms = int(np.ceil(maxtm / self.pdist))
        tms = [self.pdist*t for t in range(ntms)]
        def f(t): return(self.make_path_sing_time(t,verbose))
        if self.ncpu > 2:
            pths = async_map(f,tms,self.ncpu)
        else:
            pths = map(f,tms)
        for t,p in zip(tms,pths):
            rndtm = str(t)
            self.paths[rndtm] = p

    def make_path_sing_time(self,t,verbose = False):
        tab = self.trial.makeTable()
        while tab.tm < (t - 1e-8): # Adjustment for rounding errors
            tab.step(self.pdist)
        return map(lambda x: Path(tab,self.kv,self.kb,self.km,self.pe,self.time,self.res,verbose=verbose,\
                                  allow_timeout=self.timeout, constrained_bounce=self.cons_bounce,
                                  constrained_move=self.cons_move), range(self.npaths))

    def compress_paths(self):
        if not self.compressed:
            for k in self.paths.keys():
                for p in self.paths[k]:
                    p.compress()
            self.compressed = True
        else:
            print ("Paths already compresseed")

    def decompress_paths(self):
        if self.compressed:
            for k in self.paths.keys():
                for p in self.paths[k]:
                    p.decompress()
            self.compressed = False
        else:
            print ("Paths already decompressed")

    def save(self, flnm, docompress=True):
        cp = copy.deepcopy(self)
        if docompress:
            cp.compress_paths()
        fl = open(flnm,'w')
        pickle.dump(cp,fl,protocol=2)
        fl.close()

    # Simple way of pulling out a few outcomes and/or paths
    def get_outcomes(self,time,n,replace=False):
        pths = self.paths[str(float(time))]
        if replace:
            rs = _sample_w_rep(pths, n)
        else:
            rs = random.sample(pths,n)
        return [r.o for r in rs]

    def get_paths(self,time,n, replace=False):
        pths = self.paths[str(float(time))]
        if replace:
            rs = _sample_w_rep(pths, n)
        else:
            rs = random.sample(pths,n)
        return [r.p for r in rs]

    def get_paths_and_outcomes(self,time,n, replace=False):
        pths = self.paths[str(float(time))]
        if replace:
            rs = _sample_w_rep(pths, n)
        else:
            rs = random.sample(pths,n)
        return [(r.o,r.p) for r in rs]

    def get_single_path(self, time):
        pths = self.paths[str(float(time))]
        return random.choice(pths)

    def get_outcomes_and_bounces(self,time,n, replace=False):
        pths = self.paths[str(float(time))]
        if replace:
            rs = _sample_w_rep(pths, n)
        else:
            rs = random.sample(pths,n)
        return [(r.o,r.b) for r in rs]

    def get_outcomes_bounces_time(self, time, n, replace=False):
        pths = self.paths[str(float(time))]
        if replace:
            rs = _sample_w_rep(pths, n)
        else:
            rs = random.sample(pths,n)
        return [(r.o, r.b, len(r.p)*self.pdist) for r in rs]

    # Get maximum time allowable
    def _get_max_time(self):
        return self.maxtm

    max_time = property(_get_max_time)

def load_path_maker(flnm):
    fl = open(flnm,'rU')
    pm = pickle.load(fl)
    fl.close()
    if pm.compressed:
        pm.decompress_paths()
    return pm

# Like the PathMaker class but does things on the fly rather than storing
#  Used to slot into models that assume you have a PathMaker object creating paths
class PseudoPathMaker(PathMaker):
    def __init__(self, trial, kapv=KAPV_DEF, kapb=KAPB_DEF, kapm=KAPM_DEF, perr=PERR_DEF, npaths=None,
                 pathdist=0.1, timelen=60., timeres=0.05, cpus=1, verbose=False, allow_timeout=False,
                 constrained_bounce=False, constrained_move=False):
        self.trial=trial
        self.kv = kapv
        self.kb = kapb
        self.km = kapm
        self.pe = perr
        self.time = timelen
        self.res = timeres
        self.pdist = pathdist
        self.timeout = allow_timeout
        self.compressed = False
        self.cons_bounce = constrained_bounce
        self.cons_move = constrained_move

        # Figure out the maximum time
        tab = self.trial.makeTable()
        self.maxtm = 0
        while tab.step(self.pdist) is None:
            self.maxtm += self.pdist

    def make_paths(self, verbose=False):
        print ("Making paths not needed for PseudoPathMaker")

    def make_path_sing_time(self,t,verbose = False):
        print ("Making paths not needed for PseudoPathMaker")

    def compress_paths(self):
        print ("PsuedoPathMaker paths are not compressable")

    def decompress_paths(self):
        print ("PsuedoPathMaker paths are not compressable")

    def save(self, flnm, docompress=True):
        with open(flnm, 'w') as ofl:
            pickle.dump(self, fl, protocol=2)

    def _make_pseudo_path(self, tab):
        ntab = make_noisy(tab, self.kv, self.kb, self.km , self.pe, constrained_bounce=self.cons_bounce,
                         constrained_move=self.cons_move)
        o, p, b = ntab.simulate(self.time, self.res, True, True)

        while ((o is TIMEUP and not self.timeout) or o is OUTOFBOUNDS or ntab.balls.bounces > 255):
            del ntab
            ntab = make_noisy(tab, self.kv, self.kb, self.km , self.pe, constrained_bounce=self.cons_bounce,
                             constrained_move=self.cons_move)
            o, p, b = ntab.simulate(self.time, self.res, True, True)

        return o, p, b

    def _advance_table(self, time):
        tab = self.trial.makeTable()
        if time > 0:
            tab.step(time)
        return tab

    def get_outcomes(self,time,n):
        tab = self._advance_table(time)
        pths = [self._make_pseudo_path(tab) for _ in range(n)]
        return zip(*pths)[0]

    def get_paths(self, time, n):
        tab = self._advance_table(time)
        pths = [self._make_pseudo_path(tab) for _ in range(n)]
        return zip(*pths)[1]

    def get_paths_and_outcomes(self,time,n):
        tab = self._advance_table(time)
        pths = [self._make_pseudo_path(tab) for _ in range(n)]
        return [p[:2] for p in pths]

    def get_outcomes_and_bounces(self,time,n):
        tab = self._advance_table(time)
        pths = [self._make_pseudo_path(tab) for _ in range(n)]
        return [(p[0], p[2]) for p in pths]

    def get_outcomes_bounces_time(self, time, n):
        tab = self._advance_table(time)
        pths = [self._make_pseudo_path(tab) for _ in range(n)]
        return [(p[0], p[2], len(p[1])*self.pdist) for p in pths]

    def get_single_path(self, time):
        tab = self._advance_table(time)
        return Path(tab, self.kv, self.kb, self.km, self.pe, self.time, self.res, allow_timeout=self.timeout,
                    constrained_bounce=self.cons_bounce, constrained_move=self.cons_move)

    def _get_max_time(self):
        return self.maxtm
    max_time = property(_get_max_time)
