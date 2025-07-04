#!/usr/bin/env python
# Copyright 2021-2022 The PySCF Developers. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# Author: Qiming Sun <osirpt.sun@gmail.com>
#

'''
TDA and TDHF for no-pair DKS Hamiltonian
'''

from functools import reduce
import numpy
from pyscf import lib
from pyscf import scf
from pyscf import ao2mo
from pyscf.lib import logger
from pyscf.tdscf import rhf
from pyscf.tdscf._lr_eig import eigh as lr_eigh, eig as lr_eig
from pyscf import __config__

OUTPUT_THRESHOLD = getattr(__config__, 'tdscf_uhf_get_nto_threshold', 0.3)
REAL_EIG_THRESHOLD = getattr(__config__, 'tdscf_uhf_TDDFT_pick_eig_threshold', 1e-4)

def gen_tda_operation(mf, fock_ao=None, with_nlc=True):
    '''A x
    '''
gen_tda_hop = gen_tda_operation

def _gen_tda_operation(td, fock_ao=None):
    assert fock_ao is None
    mf = td._scf
    mo_coeff = mf.mo_coeff
    mo_energy = mf.mo_energy
    mo_occ = mf.mo_occ
    nao, nmo = mo_coeff.shape
    # Remove all negative states
    n2c = nmo // 2
    occidx = n2c + numpy.where(mo_occ[n2c:] == 1)[0]
    viridx = n2c + numpy.where(mo_occ[n2c:] == 0)[0]
    nocc = len(occidx)
    nvir = len(viridx)
    orbv = mo_coeff[:,viridx]
    orbo = mo_coeff[:,occidx]

    e_ia = hdiag = mo_energy[viridx] - mo_energy[occidx,None]
    hdiag = hdiag.ravel()

    mo_coeff = numpy.asarray(numpy.hstack((orbo,orbv)), order='F')
    vresp = td.gen_response(hermi=0)

    def vind(zs):
        zs = numpy.asarray(zs).reshape(-1,nocc,nvir)
        dms = lib.einsum('xov,pv,qo->xpq', zs, orbv, orbo.conj())
        v1ao = vresp(dms)
        v1mo = lib.einsum('xpq,qo,pv->xov', v1ao, orbo, orbv.conj())
        v1mo += numpy.einsum('xia,ia->xia', zs, e_ia)
        return v1mo.reshape(v1mo.shape[0], -1)

    return vind, hdiag

def get_ab(mf, mo_energy=None, mo_coeff=None, mo_occ=None):
    r'''A and B matrices for TDDFT response function.

    A[i,a,j,b] = \delta_{ab}\delta_{ij}(E_a - E_i) + (ai||jb)
    B[i,a,j,b] = (ai||bj)
    '''
    if mo_energy is None: mo_energy = mf.mo_energy
    if mo_coeff is None: mo_coeff = mf.mo_coeff
    if mo_occ is None: mo_occ = mf.mo_occ

    mol = mf.mol
    nao, nmo = mo_coeff.shape
    n2c = nmo // 2
    occidx = n2c + numpy.where(mo_occ[n2c:] == 1)[0]
    viridx = n2c + numpy.where(mo_occ[n2c:] == 0)[0]
    orbv = mo_coeff[:,viridx]
    orbo = mo_coeff[:,occidx]
    nvir = orbv.shape[1]
    nocc = orbo.shape[1]
    nmo = nocc + nvir
    mo = numpy.hstack((orbo, orbv))
    c1 = .5 / lib.param.LIGHT_SPEED
    moL = numpy.asarray(mo[:n2c], order='F')
    moS = numpy.asarray(mo[n2c:], order='F') * c1
    orboL = moL[:,:nocc]
    orboS = moS[:,:nocc]

    e_ia = mo_energy[viridx] - mo_energy[occidx,None]
    a = numpy.diag(e_ia.ravel()).reshape(nocc,nvir,nocc,nvir)
    b = numpy.zeros_like(a)

    def add_hf_(a, b, hyb=1):
        eri_mo = ao2mo.kernel(mol, [orboL, moL, moL, moL], intor='int2e_spinor')
        eri_mo+= ao2mo.kernel(mol, [orboS, moS, moS, moS], intor='int2e_spsp1spsp2_spinor')
        eri_mo+= ao2mo.kernel(mol, [orboS, moS, moL, moL], intor='int2e_spsp1_spinor')
        eri_mo+= ao2mo.kernel(mol, [moS, moS, orboL, moL], intor='int2e_spsp1_spinor').T
        eri_mo = eri_mo.reshape(nocc,nmo,nmo,nmo)

        a = a + numpy.einsum('iabj->iajb', eri_mo[:nocc,nocc:,nocc:,:nocc].conj())
        a = a - numpy.einsum('ijba->iajb', eri_mo[:nocc,:nocc,nocc:,nocc:].conj()) * hyb
        b = b + numpy.einsum('iajb->iajb', eri_mo[:nocc,nocc:,:nocc,nocc:].conj())
        b = b - numpy.einsum('jaib->iajb', eri_mo[:nocc,nocc:,:nocc,nocc:].conj()) * hyb
        return a, b

    if isinstance(mf, scf.hf.KohnShamDFT):
        from pyscf.dft import xc_deriv
        ni = mf._numint
        ni.libxc.test_deriv_order(mf.xc, 2, raise_error=True)
        if mf.do_nlc():
            raise NotImplementedError('DKS-TDDFT for NLC functionals')

        omega, alpha, hyb = ni.rsh_and_hybrid_coeff(mf.xc, mol.spin)
        assert omega == 0.

        a, b = add_hf_(a, b, hyb)

        xctype = ni._xc_type(mf.xc)
        dm0 = mf.make_rdm1(mo_coeff, mo_occ)
        mem_now = lib.current_memory()[0]
        max_memory = max(2000, mf.max_memory*.8-mem_now)

        def get_mo_value(ao):
            aoLa, aoLb, aoSa, aoSb = ao
            if aoLa.ndim == 2:
                moLa = lib.einsum('rp,pi->ri', aoLa, moL)
                moLb = lib.einsum('rp,pi->ri', aoLb, moL)
                moSa = lib.einsum('rp,pi->ri', aoSa, moS)
                moSb = lib.einsum('rp,pi->ri', aoSb, moS)
                return (moLa[:,:nocc], moLa[:,nocc:], moLb[:,:nocc], moLb[:,nocc:],
                        moSa[:,:nocc], moSa[:,nocc:], moSb[:,:nocc], moSb[:,nocc:])
            else:
                moLa = lib.einsum('xrp,pi->xri', aoLa, moL)
                moLb = lib.einsum('xrp,pi->xri', aoLb, moL)
                moSa = lib.einsum('xrp,pi->xri', aoSa, moS)
                moSb = lib.einsum('xrp,pi->xri', aoSb, moS)
                return (moLa[:,:,:nocc], moLa[:,:,nocc:], moLb[:,:,:nocc], moLb[:,:,nocc:],
                        moSa[:,:,:nocc], moSa[:,:,nocc:], moSb[:,:,:nocc], moSb[:,:,nocc:])

        def ud2tm(aa, ab, ba, bb):
            return numpy.stack([aa + bb,        # rho
                                ba + ab,        # mx
                                (ba - ab) * 1j, # my
                                aa - bb])       # mz
        def addLS(rhoL, rhoS):
            rhoS[1:4] *= -1  # beta * Sigma
            return rhoL + rhoS

        if xctype == 'LDA':
            ao_deriv = 0
            for ao, mask, weight, coords \
                    in ni.block_loop(mol, mf.grids, nao, ao_deriv, max_memory,
                                     with_s=True):
                if ni.collinear[0] == 'm':
                    rho = ni.eval_rho(mol, ao, dm0, mask, xctype, hermi=1, with_lapl=False)
                    eval_xc = ni.mcfun_eval_xc_adapter(mf.xc)
                    fxc = eval_xc(mf.xc, rho, deriv=2, xctype=xctype)[2]
                    wfxc = weight * fxc.reshape(4,4,-1)
                    moLoa, moLva, moLob, moLvb, moSoa, moSva, moSob, moSvb = get_mo_value(ao)
                    rhoL_ov_aa = numpy.einsum('ri,ra->ria', moLoa.conj(), moLva)
                    rhoL_ov_ab = numpy.einsum('ri,ra->ria', moLoa.conj(), moLvb)
                    rhoL_ov_ba = numpy.einsum('ri,ra->ria', moLob.conj(), moLva)
                    rhoL_ov_bb = numpy.einsum('ri,ra->ria', moLob.conj(), moLvb)
                    rhoS_ov_aa = numpy.einsum('ri,ra->ria', moSoa.conj(), moSva)
                    rhoS_ov_ab = numpy.einsum('ri,ra->ria', moSoa.conj(), moSvb)
                    rhoS_ov_ba = numpy.einsum('ri,ra->ria', moSob.conj(), moSva)
                    rhoS_ov_bb = numpy.einsum('ri,ra->ria', moSob.conj(), moSvb)
                    rhoL_ov = ud2tm(rhoL_ov_aa, rhoL_ov_ab, rhoL_ov_ba, rhoL_ov_bb)
                    rhoS_ov = ud2tm(rhoS_ov_aa, rhoS_ov_ab, rhoS_ov_ba, rhoS_ov_bb)
                    rho_ov = addLS(rhoL_ov, rhoS_ov)
                    rho_vo = rho_ov.conj()
                    w_vo = numpy.einsum('tsr,tria->sria', wfxc, rho_vo)
                    a += lib.einsum('sria,srjb->iajb', w_vo, rho_ov)
                    b += lib.einsum('sria,srjb->iajb', w_vo, rho_vo)
                elif ni.collinear[0] == 'c':
                    rho = ni.eval_rho(mol, ao, dm0, mask, xctype, hermi=1, with_lapl=False)
                    fxc = ni.eval_xc_eff(mf.xc, rho, deriv=2)[2]
                    wv_a, wv_b = weight * fxc.reshape(2,2,-1)
                    moLoa, moLva, moLob, moLvb, moSoa, moSva, moSob, moSvb = get_mo_value(ao)
                    rhoL_ov_a = numpy.einsum('ri,ra->ria', moLoa.conj(), moLva)
                    rhoL_ov_b = numpy.einsum('ri,ra->ria', moLob.conj(), moLvb)
                    rhoS_ov_a = numpy.einsum('ri,ra->ria', moSoa.conj(), moSva)
                    rhoS_ov_b = numpy.einsum('ri,ra->ria', moSob.conj(), moSvb)
                    rhoL_vo_a = rhoL_ov_a.conj()
                    rhoL_vo_b = rhoL_ov_b.conj()
                    rhoS_vo_a = rhoS_ov_a.conj()
                    rhoS_vo_b = rhoS_ov_b.conj()
                    w_vo  = wv_a[:,:,None,None] * rhoL_vo_a
                    w_vo += wv_b[:,:,None,None] * rhoL_vo_b
                    w_vo += wv_b[:,:,None,None] * rhoS_vo_a  # for beta*Sigma
                    w_vo += wv_a[:,:,None,None] * rhoS_vo_b
                    wa_vo, wb_vo = w_vo
                    a += lib.einsum('ria,rjb->iajb', wa_vo, rhoL_ov_a)
                    a += lib.einsum('ria,rjb->iajb', wb_vo, rhoL_ov_b)
                    a += lib.einsum('ria,rjb->iajb', wb_vo, rhoS_ov_a)
                    a += lib.einsum('ria,rjb->iajb', wa_vo, rhoS_ov_b)
                    b += lib.einsum('ria,rjb->iajb', wa_vo, rhoL_vo_a)
                    b += lib.einsum('ria,rjb->iajb', wb_vo, rhoL_vo_b)
                    b += lib.einsum('ria,rjb->iajb', wb_vo, rhoS_vo_a)
                    b += lib.einsum('ria,rjb->iajb', wa_vo, rhoS_vo_b)
                else:
                    raise NotImplementedError(ni.collinear)

        elif xctype == 'GGA':
            ao_deriv = 1
            for ao, mask, weight, coords \
                    in ni.block_loop(mol, mf.grids, nao, ao_deriv, max_memory,
                                     with_s=True):
                if ni.collinear[0] == 'm':
                    rho = ni.eval_rho(mol, ao, dm0, mask, xctype, hermi=1, with_lapl=False)
                    eval_xc = ni.mcfun_eval_xc_adapter(mf.xc)
                    fxc = eval_xc(mf.xc, rho, deriv=2, xctype=xctype)[2]
                    wfxc = weight * fxc
                    moLoa, moLva, moLob, moLvb, moSoa, moSva, moSob, moSvb = get_mo_value(ao)
                    rhoL_ov_aa = numpy.einsum('ri,xra->xria', moLoa[0].conj(), moLva)
                    rhoL_ov_ab = numpy.einsum('ri,xra->xria', moLoa[0].conj(), moLvb)
                    rhoL_ov_ba = numpy.einsum('ri,xra->xria', moLob[0].conj(), moLva)
                    rhoL_ov_bb = numpy.einsum('ri,xra->xria', moLob[0].conj(), moLvb)
                    rhoS_ov_aa = numpy.einsum('ri,xra->xria', moSoa[0].conj(), moSva)
                    rhoS_ov_ab = numpy.einsum('ri,xra->xria', moSoa[0].conj(), moSvb)
                    rhoS_ov_ba = numpy.einsum('ri,xra->xria', moSob[0].conj(), moSva)
                    rhoS_ov_bb = numpy.einsum('ri,xra->xria', moSob[0].conj(), moSvb)
                    rhoL_ov_aa[1:4] += numpy.einsum('xri,ra->xria', moLoa[1:4].conj(), moLva[0])
                    rhoL_ov_ab[1:4] += numpy.einsum('xri,ra->xria', moLoa[1:4].conj(), moLvb[0])
                    rhoL_ov_ba[1:4] += numpy.einsum('xri,ra->xria', moLob[1:4].conj(), moLva[0])
                    rhoL_ov_bb[1:4] += numpy.einsum('xri,ra->xria', moLob[1:4].conj(), moLvb[0])
                    rhoS_ov_aa[1:4] += numpy.einsum('xri,ra->xria', moSoa[1:4].conj(), moSva[0])
                    rhoS_ov_ab[1:4] += numpy.einsum('xri,ra->xria', moSoa[1:4].conj(), moSvb[0])
                    rhoS_ov_ba[1:4] += numpy.einsum('xri,ra->xria', moSob[1:4].conj(), moSva[0])
                    rhoS_ov_bb[1:4] += numpy.einsum('xri,ra->xria', moSob[1:4].conj(), moSvb[0])
                    rhoL_ov = ud2tm(rhoL_ov_aa, rhoL_ov_ab, rhoL_ov_ba, rhoL_ov_bb)
                    rhoS_ov = ud2tm(rhoS_ov_aa, rhoS_ov_ab, rhoS_ov_ba, rhoS_ov_bb)
                    rho_ov = addLS(rhoL_ov, rhoS_ov)
                    rho_vo = rho_ov.conj()
                    w_vo = numpy.einsum('txsyr,txria->syria', wfxc, rho_vo)
                    a += lib.einsum('syria,syrjb->iajb', w_vo, rho_ov)
                    b += lib.einsum('syria,syrjb->iajb', w_vo, rho_vo)
                elif ni.collinear[0] == 'c':
                    rho = ni.eval_rho(mol, ao, dm0, mask, xctype, hermi=1, with_lapl=False)
                    fxc = ni.eval_xc_eff(mf.xc, rho, deriv=2)[2]
                    fxc = xc_deriv.ud2ts(fxc)
                    wfxc = weight * fxc
                    moLoa, moLva, moLob, moLvb, moSoa, moSva, moSob, moSvb = get_mo_value(ao)
                    rhoL_ov_a = numpy.einsum('xri,ra->xria', moLoa.conj(), moLva[0])
                    rhoL_ov_b = numpy.einsum('xri,ra->xria', moLob.conj(), moLvb[0])
                    rhoL_ov_a[1:4] += numpy.einsum('ri,xra->xria', moLoa[0].conj(), moLva[1:4])
                    rhoL_ov_b[1:4] += numpy.einsum('ri,xra->xria', moLob[0].conj(), moLvb[1:4])
                    rhoS_ov_a = numpy.einsum('xri,ra->xria', moSoa.conj(), moSva[0])
                    rhoS_ov_b = numpy.einsum('xri,ra->xria', moSob.conj(), moSvb[0])
                    rhoS_ov_a[1:4] += numpy.einsum('ri,xra->xria', moSoa[0].conj(), moSva[1:4])
                    rhoS_ov_b[1:4] += numpy.einsum('ri,xra->xria', moSob[0].conj(), moSvb[1:4])
                    rhoL_ov = numpy.stack((rhoL_ov_a+rhoL_ov_b, rhoL_ov_a-rhoL_ov_b))
                    rhoS_ov = numpy.stack((rhoS_ov_a+rhoS_ov_b, rhoS_ov_a-rhoS_ov_b))
                    rhoS_ov[1] *= -1
                    rho_ov = rhoL_ov + rhoS_ov
                    rho_vo = rho_ov.conj()
                    w_vo = numpy.einsum('txsyr,txria->syria', wfxc, rho_vo)
                    a += lib.einsum('syria,syrjb->iajb', w_vo, rho_ov)
                    b += lib.einsum('syria,syrjb->iajb', w_vo, rho_vo)
                else:
                    raise NotImplementedError(ni.collinear)

        elif xctype == 'HF':
            pass

        elif xctype == 'NLC':
            raise NotImplementedError('NLC')

        elif xctype == 'MGGA':
            ao_deriv = 1
            for ao, mask, weight, coords \
                    in ni.block_loop(mol, mf.grids, nao, ao_deriv, max_memory,
                                     with_s=True):
                if ni.collinear[0] == 'm':
                    rho = ni.eval_rho(mol, ao, dm0, mask, xctype, hermi=1, with_lapl=False)
                    eval_xc = ni.mcfun_eval_xc_adapter(mf.xc)
                    fxc = eval_xc(mf.xc, rho, deriv=2, xctype=xctype)[2]
                    wfxc = weight * fxc
                    moLoa, moLva, moLob, moLvb, moSoa, moSva, moSob, moSvb = get_mo_value(ao)
                    rhoL_ov_aa = numpy.einsum('ri,xra->xria', moLoa[0].conj(), moLva)
                    rhoL_ov_ab = numpy.einsum('ri,xra->xria', moLoa[0].conj(), moLvb)
                    rhoL_ov_ba = numpy.einsum('ri,xra->xria', moLob[0].conj(), moLva)
                    rhoL_ov_bb = numpy.einsum('ri,xra->xria', moLob[0].conj(), moLvb)
                    rhoS_ov_aa = numpy.einsum('ri,xra->xria', moSoa[0].conj(), moSva)
                    rhoS_ov_ab = numpy.einsum('ri,xra->xria', moSoa[0].conj(), moSvb)
                    rhoS_ov_ba = numpy.einsum('ri,xra->xria', moSob[0].conj(), moSva)
                    rhoS_ov_bb = numpy.einsum('ri,xra->xria', moSob[0].conj(), moSvb)
                    rhoL_ov_aa[1:4] += numpy.einsum('xri,ra->xria', moLoa[1:4].conj(), moLva[0])
                    rhoL_ov_ab[1:4] += numpy.einsum('xri,ra->xria', moLoa[1:4].conj(), moLvb[0])
                    rhoL_ov_ba[1:4] += numpy.einsum('xri,ra->xria', moLob[1:4].conj(), moLva[0])
                    rhoL_ov_bb[1:4] += numpy.einsum('xri,ra->xria', moLob[1:4].conj(), moLvb[0])
                    rhoS_ov_aa[1:4] += numpy.einsum('xri,ra->xria', moSoa[1:4].conj(), moSva[0])
                    rhoS_ov_ab[1:4] += numpy.einsum('xri,ra->xria', moSoa[1:4].conj(), moSvb[0])
                    rhoS_ov_ba[1:4] += numpy.einsum('xri,ra->xria', moSob[1:4].conj(), moSva[0])
                    rhoS_ov_bb[1:4] += numpy.einsum('xri,ra->xria', moSob[1:4].conj(), moSvb[0])
                    tauL_ov_aa = numpy.einsum('xri,xra->ria', moLoa[1:4].conj(), moLva[1:4]) * .5
                    tauL_ov_ab = numpy.einsum('xri,xra->ria', moLoa[1:4].conj(), moLvb[1:4]) * .5
                    tauL_ov_ba = numpy.einsum('xri,xra->ria', moLob[1:4].conj(), moLva[1:4]) * .5
                    tauL_ov_bb = numpy.einsum('xri,xra->ria', moLob[1:4].conj(), moLvb[1:4]) * .5
                    tauS_ov_aa = numpy.einsum('xri,xra->ria', moSoa[1:4].conj(), moSva[1:4]) * .5
                    tauS_ov_ab = numpy.einsum('xri,xra->ria', moSoa[1:4].conj(), moSvb[1:4]) * .5
                    tauS_ov_ba = numpy.einsum('xri,xra->ria', moSob[1:4].conj(), moSva[1:4]) * .5
                    tauS_ov_bb = numpy.einsum('xri,xra->ria', moSob[1:4].conj(), moSvb[1:4]) * .5
                    rhoL_ov_aa = numpy.vstack([rhoL_ov_aa, tauL_ov_aa[numpy.newaxis]])
                    rhoL_ov_ab = numpy.vstack([rhoL_ov_ab, tauL_ov_ab[numpy.newaxis]])
                    rhoL_ov_ba = numpy.vstack([rhoL_ov_ba, tauL_ov_ba[numpy.newaxis]])
                    rhoL_ov_bb = numpy.vstack([rhoL_ov_bb, tauL_ov_bb[numpy.newaxis]])
                    rhoS_ov_aa = numpy.vstack([rhoS_ov_aa, tauS_ov_aa[numpy.newaxis]])
                    rhoS_ov_ab = numpy.vstack([rhoS_ov_ab, tauS_ov_ab[numpy.newaxis]])
                    rhoS_ov_ba = numpy.vstack([rhoS_ov_ba, tauS_ov_ba[numpy.newaxis]])
                    rhoS_ov_bb = numpy.vstack([rhoS_ov_bb, tauS_ov_bb[numpy.newaxis]])
                    rhoL_ov = ud2tm(rhoL_ov_aa, rhoL_ov_ab, rhoL_ov_ba, rhoL_ov_bb)
                    rhoS_ov = ud2tm(rhoS_ov_aa, rhoS_ov_ab, rhoS_ov_ba, rhoS_ov_bb)
                    rho_ov = addLS(rhoL_ov, rhoS_ov)
                    rho_vo = rho_ov.conj()
                    w_vo = numpy.einsum('txsyr,txria->syria', wfxc, rho_vo)
                    a += lib.einsum('syria,syrjb->iajb', w_vo, rho_ov)
                    b += lib.einsum('syria,syrjb->iajb', w_vo, rho_vo)
                elif ni.collinear[0] == 'c':
                    rho = ni.eval_rho(mol, ao, dm0, mask, xctype, hermi=1, with_lapl=False)
                    fxc = ni.eval_xc_eff(mf.xc, rho, deriv=2)[2]
                    fxc = xc_deriv.ud2ts(fxc)
                    wfxc = weight * fxc
                    moLoa, moLva, moLob, moLvb, moSoa, moSva, moSob, moSvb = get_mo_value(ao)
                    rhoL_ov_a = numpy.einsum('xri,ra->xria', moLoa.conj(), moLva[0])
                    rhoL_ov_b = numpy.einsum('xri,ra->xria', moLob.conj(), moLvb[0])
                    rhoS_ov_a = numpy.einsum('xri,ra->xria', moSoa.conj(), moSva[0])
                    rhoS_ov_b = numpy.einsum('xri,ra->xria', moSob.conj(), moSvb[0])
                    rhoL_ov_a[1:4] += numpy.einsum('ri,xra->xria', moLoa[0].conj(), moLva[1:4])
                    rhoL_ov_b[1:4] += numpy.einsum('ri,xra->xria', moLob[0].conj(), moLvb[1:4])
                    rhoS_ov_a[1:4] += numpy.einsum('ri,xra->xria', moSoa[0].conj(), moSva[1:4])
                    rhoS_ov_b[1:4] += numpy.einsum('ri,xra->xria', moSob[0].conj(), moSvb[1:4])
                    tauL_ov_a = numpy.einsum('xri,xra->ria', moLoa[1:4].conj(), moLva[1:4]) * .5
                    tauL_ov_b = numpy.einsum('xri,xra->ria', moLob[1:4].conj(), moLvb[1:4]) * .5
                    tauS_ov_a = numpy.einsum('xri,xra->ria', moSoa[1:4].conj(), moSva[1:4]) * .5
                    tauS_ov_b = numpy.einsum('xri,xra->ria', moSob[1:4].conj(), moSvb[1:4]) * .5
                    rhoL_ov_a = numpy.vstack([rhoL_ov_a, tauL_ov_a[numpy.newaxis]])
                    rhoL_ov_b = numpy.vstack([rhoL_ov_b, tauL_ov_b[numpy.newaxis]])
                    rhoS_ov_a = numpy.vstack([rhoS_ov_a, tauS_ov_a[numpy.newaxis]])
                    rhoS_ov_b = numpy.vstack([rhoS_ov_b, tauS_ov_b[numpy.newaxis]])
                    rhoL_ov = numpy.stack((rhoL_ov_a+rhoL_ov_b, rhoL_ov_a-rhoL_ov_b))
                    rhoS_ov = numpy.stack((rhoS_ov_a+rhoS_ov_b, rhoS_ov_a-rhoS_ov_b))
                    rhoS_ov[1] *= -1
                    rho_ov = rhoL_ov + rhoS_ov
                    rho_vo = rho_ov.conj()
                    w_vo = numpy.einsum('txsyr,txria->syria', wfxc, rho_vo)
                    a += lib.einsum('syria,syrjb->iajb', w_vo, rho_ov)
                    b += lib.einsum('syria,syrjb->iajb', w_vo, rho_vo)
                else:
                    raise NotImplementedError(ni.collinear)

    else:
        a, b = add_hf_(a, b)

    return a, b

def get_nto(tdobj, state=1, threshold=OUTPUT_THRESHOLD, verbose=None):
    raise NotImplementedError('get_nto')

def analyze(tdobj, verbose=None):
    raise NotImplementedError('analyze')

def _contract_multipole(tdobj, ints, hermi=True, xy=None):
    raise NotImplementedError


class TDBase(rhf.TDBase):

    @lib.with_doc(get_ab.__doc__)
    def get_ab(self, mf=None):
        if mf is None: mf = self._scf
        return get_ab(mf)

    analyze = analyze
    get_nto = get_nto
    _contract_multipole = _contract_multipole  # needed by transition dipoles

    def nuc_grad_method(self):
        raise NotImplementedError


@lib.with_doc(rhf.TDA.__doc__)
class TDA(TDBase):

    singlet = None

    def gen_vind(self, mf=None):
        '''Generate function to compute Ax'''
        assert mf is None or mf is self._scf
        return _gen_tda_operation(self)

    def get_init_guess(self, mf, nstates=None, wfnsym=None):
        if nstates is None: nstates = self.nstates
        mo_energy = mf.mo_energy
        mo_occ = mf.mo_occ
        # Remove all negative states
        n2c = mf.mo_occ.size // 2
        occidx = n2c + numpy.where(mo_occ[n2c:] == 1)[0]
        viridx = n2c + numpy.where(mo_occ[n2c:] == 0)[0]
        e_ia = mo_energy[viridx] - mo_energy[occidx,None]

        nov = e_ia.size
        nstates = min(nstates, nov)
        e_ia = e_ia.ravel()
        e_threshold = numpy.sort(e_ia)[nstates-1]
        e_threshold += self.deg_eia_thresh

        idx = numpy.where(e_ia <= e_threshold)[0]
        x0 = numpy.zeros((idx.size, nov))
        for i, j in enumerate(idx):
            x0[i, j] = 1  # Koopmans' excitations
        return x0

    def kernel(self, x0=None, nstates=None):
        '''TDA diagonalization solver
        '''
        cpu0 = (logger.process_clock(), logger.perf_counter())
        self.check_sanity()
        self.dump_flags()
        if nstates is None:
            nstates = self.nstates
        else:
            self.nstates = nstates

        log = logger.Logger(self.stdout, self.verbose)

        vind, hdiag = self.gen_vind(self._scf)
        precond = self.get_precond(hdiag)

        def pickeig(w, v, nroots, envs):
            idx = numpy.where(w > self.positive_eig_threshold)[0]
            return w[idx], v[:,idx], idx

        if x0 is None:
            x0 = self.get_init_guess(self._scf, self.nstates)

        self.converged, self.e, x1 = lr_eigh(
            vind, x0, precond, tol_residual=self.conv_tol, lindep=self.lindep,
            nroots=nstates, pick=pickeig, max_cycle=self.max_cycle,
            max_memory=self.max_memory, verbose=log)

        nocc = (self._scf.mo_occ>0).sum()
        nmo = self._scf.mo_occ.size
        n2c = nmo // 2
        nvir = n2c - nocc
        self.xy = [(xi.reshape(nocc,nvir), 0) for xi in x1]

        if self.chkfile:
            lib.chkfile.save(self.chkfile, 'tddft/e', self.e)
            lib.chkfile.save(self.chkfile, 'tddft/xy', self.xy)

        log.timer('TDA', *cpu0)
        self._finalize()
        return self.e, self.xy


def gen_tdhf_operation(mf, fock_ao=None, with_nlc=True):
    '''Generate function to compute

    [ A   B ][X]
    [-B* -A*][Y]
    '''
    td = TDHF(mf)
    td.exclude_nlc = not with_nlc
    return _gen_tdhf_operation(td, fock_ao)

def _gen_tdhf_operation(td, fock_ao=None):
    mf = td._scf
    mo_coeff = mf.mo_coeff
    mo_energy = mf.mo_energy
    mo_occ = mf.mo_occ
    nao, nmo = mo_coeff.shape
    n2c = nmo // 2
    occidx = n2c + numpy.where(mo_occ[n2c:] == 1)[0]
    viridx = n2c + numpy.where(mo_occ[n2c:] == 0)[0]
    nocc = len(occidx)
    nvir = len(viridx)
    orbv = mo_coeff[:,viridx]
    orbo = mo_coeff[:,occidx]

    assert fock_ao is None
    e_ia = hdiag = mo_energy[viridx] - mo_energy[occidx,None]
    hdiag = numpy.hstack((hdiag.ravel(), -hdiag.ravel())).real

    mo_coeff = numpy.asarray(numpy.hstack((orbo,orbv)), order='F')
    vresp = td.gen_response(hermi=0)

    def vind(xys):
        xys = numpy.asarray(xys).reshape(-1,2,nocc,nvir)
        xs, ys = xys.transpose(1,0,2,3)
        dms  = lib.einsum('xov,pv,qo->xpq', xs, orbv, orbo.conj())
        dms += lib.einsum('xov,qv,po->xpq', ys, orbv.conj(), orbo)
        v1ao = vresp(dms) # = <mj||nb> Xjb + <mb||nj> Yjb
        # A ~= <aj||ib>, B = <ab||ij>
        # AX + BY
        # = <aj||ib> Xjb + <ab||ij> Yjb
        # = (<mj||nb> Xjb + <mb||nj> Yjb) Cma* Cni
        v1_top = lib.einsum('xpq,qo,pv->xov', v1ao, orbo, orbv.conj())
        # (B*)X + (A*)Y
        # = <ij||ab> Xjb + <ib||aj> Yjb
        # = (<mj||nb> Xjb + <mb||nj> Yjb) Cmi* Cna
        v1_bot = lib.einsum('xpq,po,qv->xov', v1ao, orbo.conj(), orbv)
        v1_top += numpy.einsum('xia,ia->xia', xs, e_ia)  # AX
        v1_bot += numpy.einsum('xia,ia->xia', ys, e_ia)  # (A*)Y

        # (AX, (-A*)Y)
        nz = xys.shape[0]
        hx = numpy.hstack((v1_top.reshape(nz,-1), -v1_bot.reshape(nz,-1)))
        return hx

    return vind, hdiag


class TDHF(TDBase):

    singlet = None

    @lib.with_doc(gen_tdhf_operation.__doc__)
    def gen_vind(self, mf=None):
        assert mf is None or mf is self._scf
        return _gen_tdhf_operation(self)

    def get_init_guess(self, mf, nstates=None, wfnsym=None):
        x0 = TDA.get_init_guess(self, mf, nstates, wfnsym)
        y0 = numpy.zeros_like(x0)
        return numpy.hstack([x0, y0])

    get_precond = rhf.TDHF.get_precond

    def kernel(self, x0=None, nstates=None):
        '''TDHF diagonalization with non-Hermitian eigenvalue solver
        '''
        cpu0 = (logger.process_clock(), logger.perf_counter())
        self.check_sanity()
        self.dump_flags()
        if nstates is None:
            nstates = self.nstates
        else:
            self.nstates = nstates

        log = logger.Logger(self.stdout, self.verbose)

        vind, hdiag = self.gen_vind(self._scf)
        precond = self.get_precond(hdiag)

        def pickeig(w, v, nroots, envs):
            realidx = numpy.where((abs(w.imag) < REAL_EIG_THRESHOLD) &
                                  (w.real > self.positive_eig_threshold))[0]
            # FIXME: Should the amplitudes be real?
            return lib.linalg_helper._eigs_cmplx2real(w, v, realidx,
                                                      real_eigenvectors=False)

        if x0 is None:
            x0 = self.get_init_guess(self._scf, self.nstates)

        self.converged, w, x1 = lr_eig(
            vind, x0, precond, tol_residual=self.conv_tol, lindep=self.lindep,
            nroots=nstates, pick=pickeig, max_cycle=self.max_cycle,
            max_memory=self.max_memory, verbose=log)

        nocc = (self._scf.mo_occ>0).sum()
        nmo = self._scf.mo_occ.size
        n2c = nmo // 2
        nvir = n2c - nocc
        self.e = w
        def norm_xy(z):
            x, y = z.reshape(2,nocc,nvir)
            norm = lib.norm(x)**2 - lib.norm(y)**2
            if norm < 0:
                log.warn('TDDFT amplitudes |X| smaller than |Y|')
            norm = abs(norm)**-.5
            return x*norm, y*norm
        self.xy = [norm_xy(z) for z in x1]

        if self.chkfile:
            lib.chkfile.save(self.chkfile, 'tddft/e', self.e)
            lib.chkfile.save(self.chkfile, 'tddft/xy', self.xy)

        log.timer('TDDFT', *cpu0)
        self._finalize()
        return self.e, self.xy

scf.dhf.DHF.TDA  = scf.dhf.RDHF.TDA  = lib.class_as_method(TDA)
scf.dhf.DHF.TDHF = scf.dhf.RDHF.TDHF = lib.class_as_method(TDHF)

del (OUTPUT_THRESHOLD)
