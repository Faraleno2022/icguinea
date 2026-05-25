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


def base_onfpp_effective(bulletin, taux_onfpp=Decimal('1.5')):
    """Retourne l'assiette ONFPP exploitable, y compris pour les anciens bulletins."""
    base_onfpp = decimal_or_zero(getattr(bulletin, 'base_onfpp', None))
    if base_onfpp > 0:
        return base_onfpp

    base_vf = decimal_or_zero(getattr(bulletin, 'base_vf', None))
    if base_vf > 0:
        return base_vf

    contribution_onfpp = decimal_or_zero(getattr(bulletin, 'contribution_onfpp', None))
    taux_onfpp = decimal_or_zero(taux_onfpp)
    if contribution_onfpp > 0 and taux_onfpp > 0:
        return (contribution_onfpp * Decimal('100') / taux_onfpp).quantize(Decimal('1'))

    return Decimal('0')


def somme_base_onfpp_effective(bulletins, taux_onfpp=Decimal('1.5')):
    """Additionne l'assiette ONFPP effective bulletin par bulletin."""
    return sum(
        (base_onfpp_effective(bulletin, taux_onfpp) for bulletin in bulletins),
        Decimal('0'),
    )


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
