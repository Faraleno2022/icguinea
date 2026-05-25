"""Helpers partages pour les declarations paie."""
from decimal import Decimal


def decimal_or_zero(value):
    """Normalise une valeur numerique en Decimal."""
    if value is None:
        return Decimal('0')
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value or 0))


def taux_optimisation_base(masse_salariale, base):
    """Retourne la part du brut retiree d'une assiette."""
    masse_salariale = decimal_or_zero(masse_salariale)
    base = decimal_or_zero(base)
    if masse_salariale <= 0:
        return Decimal('0.00')
    deduction = max(Decimal('0'), masse_salariale - base)
    return (deduction * Decimal('100') / masse_salariale).quantize(Decimal('0.01'))


def analyser_bases_vf_onfpp(masse_salariale, base_vf, base_onfpp):
    """Analyse les assiettes VF et ONFPP sans les confondre."""
    masse_salariale = decimal_or_zero(masse_salariale)
    base_vf = decimal_or_zero(base_vf)
    base_onfpp = decimal_or_zero(base_onfpp) or base_vf

    taux_vf = taux_optimisation_base(masse_salariale, base_vf)
    taux_onfpp = taux_optimisation_base(masse_salariale, base_onfpp)
    bases_distinctes = abs(base_vf - base_onfpp) > Decimal('1')

    if bases_distinctes:
        mode_fiscal = 'bases_distinctes'
        mode_fiscal_label = 'Bases différenciées - VF et ONFPP sur assiettes distinctes'
    elif taux_vf > 0:
        mode_fiscal = 'optimise'
        mode_fiscal_label = 'Optimisé - base VF/ONFPP réduite des indemnités exonérées'
    else:
        mode_fiscal = 'strict'
        mode_fiscal_label = 'Strict fiscal - VF/ONFPP sur salaire brut'

    return {
        'base_vf': base_vf,
        'base_onfpp': base_onfpp,
        'bases_vf_onfpp_distinctes': bases_distinctes,
        'taux_optimisation_vf': taux_vf,
        'taux_optimisation_onfpp': taux_onfpp,
        # Compatibilite historique: l'ancien taux global etait celui de la VF.
        'taux_optimisation_global': taux_vf,
        'mode_fiscal': mode_fiscal,
        'mode_fiscal_label': mode_fiscal_label,
    }
