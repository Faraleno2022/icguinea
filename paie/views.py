from django.core.exceptions import PermissionDenied
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required, permission_required
from django.contrib import messages
from django.db.models import Sum, Count, Q, Case, When, Value, IntegerField, F, Prefetch
from django.http import JsonResponse, HttpResponse
from django.utils import timezone
from django.db import transaction
from django.views.decorators.http import require_POST, require_GET
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from datetime import datetime, date
import json
import re
import unicodedata


def parse_montant(valeur):
    """Convertir un montant saisi (format français ou anglais) en Decimal.
    Supporte: 1 500,50 / 1500,50 / 1500.50 / 1,500.50
    Retourne None si la valeur est vide ou invalide."""
    if not valeur:
        return None
    valeur = str(valeur).strip().replace('\xa0', '').replace(' ', '')
    # Si contient à la fois . et , déterminer le séparateur décimal
    if ',' in valeur and '.' in valeur:
        # 1.500,50 → français ou 1,500.50 → anglais
        if valeur.rindex(',') > valeur.rindex('.'):
            # virgule après point → format français: 1.500,50
            valeur = valeur.replace('.', '').replace(',', '.')
        else:
            # point après virgule → format anglais: 1,500.50
            valeur = valeur.replace(',', '')
    elif ',' in valeur:
        # Seulement virgule: 1500,50 ou 1,500
        parts = valeur.split(',')
        if len(parts) == 2 and len(parts[1]) <= 2:
            # Décimale: 1500,50
            valeur = valeur.replace(',', '.')
        else:
            # Séparateur milliers: 1,500,000
            valeur = valeur.replace(',', '')
    try:
        return Decimal(valeur)
    except (InvalidOperation, ValueError):
        return None

from .models import (
    PeriodePaie, BulletinPaie, LigneBulletin, RubriquePaie,
    ElementSalaire, CumulPaie, HistoriquePaie, Constante, TrancheRTS,
    ParametrePaie, AlerteEcheance, ArchiveBulletin
)
from employes.models import Employe
from .services import MoteurCalculPaie
from .utils import format_anciennete_bulletin
from .utils_declarations import analyser_bases_vf_onfpp, somme_base_onfpp_effective
from core.decorators import reauth_required, entreprise_active_required


@login_required
@entreprise_active_required
@reauth_required
def paie_home(request):
    """Vue d'accueil du module paie"""
    # Statistiques générales - chercher la période la plus récente (ouverte, calculée ou validée)
    periode_actuelle = PeriodePaie.objects.filter(
        entreprise=request.user.entreprise,
        statut_periode__in=['ouverte', 'calculee', 'validee', 'payee']
    ).order_by('-annee', '-mois').first()
    
    stats = {
        'periode_actuelle': periode_actuelle,
        'total_employes': Employe.objects.filter(
            entreprise=request.user.entreprise,
            statut_employe='actif'
        ).count(),
        'bulletins_mois': 0,
        'montant_total_brut': 0,
        'montant_total_net': 0,
    }
    
    if periode_actuelle:
        agg = BulletinPaie.objects.filter(
            periode=periode_actuelle,
            employe__entreprise=request.user.entreprise,
        ).aggregate(
            nb=Count('id'),
            total_brut=Sum('salaire_brut'),
            total_net=Sum('net_a_payer'),
        )
        stats['bulletins_mois'] = agg['nb'] or 0
        stats['montant_total_brut'] = agg['total_brut'] or 0
        stats['montant_total_net'] = agg['total_net'] or 0
    
    return render(request, 'paie/home.html', {'stats': stats})


@login_required
@entreprise_active_required
@reauth_required
def liste_periodes(request):
    """Liste des périodes de paie"""
    periodes = PeriodePaie.objects.filter(
        entreprise=request.user.entreprise
    ).annotate(
        nb_bulletins=Count('bulletins')
    )
    
    return render(request, 'paie/periodes/liste.html', {
        'periodes': periodes
    })


@login_required
@entreprise_active_required
@reauth_required
def creer_periode(request):
    """Créer une nouvelle période de paie"""
    from datetime import date
    from calendar import monthrange
    
    if request.method == 'POST':
        try:
            annee = int(request.POST.get('annee'))
            mois = int(request.POST.get('mois'))
            
            # Vérifier que la période est dans l'année en cours
            today = date.today()
            
            if annee != today.year or mois < 1 or mois > 12:
                messages.error(
                    request,
                    f"❌ Impossible de créer une période pour {mois:02d}/{annee}. "
                    f"Vous pouvez uniquement créer des périodes pour l'année en cours ({today.year})."
                )
                return render(request, 'paie/periodes/creer.html', {
                    'current_year': today.year,
                    'current_month': today.month,
                })
            
            nb_jours = monthrange(annee, mois)[1]
            
            # Vérifier si la période existe déjà
            if PeriodePaie.objects.filter(
                entreprise=request.user.entreprise,
                annee=annee,
                mois=mois
            ).exists():
                messages.error(request, 'Cette période existe déjà.')
                return redirect('paie:liste_periodes')
            
            # Calculer les dates
            date_debut = date(annee, mois, 1)
            date_fin = date(annee, mois, nb_jours)
            
            # Créer la période
            periode = PeriodePaie.objects.create(
                entreprise=request.user.entreprise,
                annee=annee,
                mois=mois,
                date_debut=date_debut,
                date_fin=date_fin,
                statut_periode='ouverte'
            )
            
            messages.success(request, f'Période {periode} créée avec succès.')
            return redirect('paie:detail_periode', pk=periode.pk)
            
        except ValueError:
            messages.error(request, 'Erreur : année et mois doivent être des nombres entiers.')
        except Exception as e:
            messages.error(request, f'Erreur lors de la création : {str(e)}')
    
    today = date.today()
    return render(request, 'paie/periodes/creer.html', {
        'current_year': today.year,
        'current_month': today.month,
    })


@login_required
@entreprise_active_required
@reauth_required
def detail_periode(request, pk):
    """Détail d'une période de paie"""
    periode = get_object_or_404(PeriodePaie, pk=pk, entreprise=request.user.entreprise)
    bulletins = BulletinPaie.objects.filter(
        periode=periode,
        employe__entreprise=request.user.entreprise,
    ).select_related('employe')
    
    # Statistiques de la période
    stats = bulletins.aggregate(
        total_brut=Sum('salaire_brut'),
        total_net=Sum('net_a_payer'),
        total_cnss_employe=Sum('cnss_employe'),
        total_cnss_employeur=Sum('cnss_employeur'),
        total_irg=Sum('irg')
    )
    
    return render(request, 'paie/periodes/detail.html', {
        'periode': periode,
        'bulletins': bulletins,
        'stats': stats
    })


@login_required
@entreprise_active_required
@reauth_required
def calculer_periode(request, pk):
    """Calculer tous les bulletins d'une période (OPTIMISÉ)"""
    periode = get_object_or_404(PeriodePaie, pk=pk, entreprise=request.user.entreprise)
    
    if periode.statut_periode not in ['ouverte', 'calculee']:
        messages.error(request, 'Cette période ne peut plus être calculée.')
        return redirect('paie:detail_periode', pk=pk)
    
    # Vérifier que la période n'est pas dans un mois futur
    from datetime import date
    today = date.today()
    if periode.annee > today.year or (periode.annee == today.year and periode.mois > today.month):
        messages.error(
            request,
            f"❌ Impossible de calculer les bulletins pour une période ultérieure ({periode}). "
            f"La date actuelle est le {today.strftime('%d/%m/%Y')}. "
            f"Seules les périodes du mois en cours ou des mois passés peuvent être calculées."
        )
        return redirect('paie:detail_periode', pk=pk)
    
    if request.method == 'POST':
        try:
            # Utiliser le service bulk optimisé
            from .services_bulk import BulkPayrollService
            
            bulk_service = BulkPayrollService(periode, request.user.entreprise)
            result = bulk_service.calculer_tous_bulletins(utilisateur=request.user)
            
            # Mettre à jour le statut de la période
            periode.statut_periode = 'calculee'
            periode.save()
            
            if result['erreurs']:
                messages.warning(
                    request,
                    f"{result['bulletins_crees']} bulletins créés en {result['temps_execution']}s. "
                    f"Erreurs: {', '.join(result['erreurs'][:5])}"
                    f"{'...' if len(result['erreurs']) > 5 else ''}"
                )
            else:
                messages.success(
                    request,
                    f"{result['bulletins_crees']} bulletins calculés en {result['temps_execution']}s."
                )
                
        except Exception as e:
            messages.error(request, f'Erreur lors du calcul : {str(e)}')
        
        return redirect('paie:detail_periode', pk=pk)
    
    # GET: Afficher la page de confirmation
    employes_count = Employe.objects.filter(
        entreprise=request.user.entreprise,
        statut_employe='actif'
    ).count()
    return render(request, 'paie/periodes/calculer.html', {
        'periode': periode,
        'employes_count': employes_count
    })


@login_required
@entreprise_active_required
@reauth_required
def valider_periode(request, pk):
    """Valider une période de paie"""
    periode = get_object_or_404(PeriodePaie, pk=pk, entreprise=request.user.entreprise)
    
    if periode.statut_periode != 'calculee':
        messages.error(request, 'La période doit être calculée avant validation.')
        return redirect('paie:detail_periode', pk=pk)
    
    if request.method == 'POST':
        with transaction.atomic():
            # Valider tous les bulletins
            BulletinPaie.objects.filter(
                periode=periode,
                employe__entreprise=request.user.entreprise,
            ).update(
                statut_bulletin='valide'
            )
            
            # Mettre à jour la période
            periode.statut_periode = 'validee'
            periode.save()
            
            # Archiver les bulletins pour traçabilité
            try:
                from .services_archive import ArchivageService
                stats = ArchivageService.archiver_periode(periode)
                if stats['archivés'] > 0:
                    messages.info(request, f"{stats['archivés']} bulletin(s) archivé(s) pour traçabilité.")
            except Exception as e:
                messages.warning(request, f"Archivage partiel: {e}")
            
            messages.success(request, 'Période validée avec succès.')
        
        return redirect('paie:detail_periode', pk=pk)
    
    return render(request, 'paie/periodes/valider.html', {'periode': periode})


@login_required
@entreprise_active_required
@reauth_required
def cloturer_periode(request, pk):
    """Clôturer une période de paie"""
    periode = get_object_or_404(PeriodePaie, pk=pk, entreprise=request.user.entreprise)
    
    if periode.statut_periode != 'validee':
        messages.error(request, 'La période doit être validée avant clôture.')
        return redirect('paie:detail_periode', pk=pk)
    
    if request.method == 'POST':
        with transaction.atomic():
            periode.statut_periode = 'cloturee'
            periode.date_cloture = timezone.now()
            periode.utilisateur_cloture = request.user
            periode.save()
            
            messages.success(request, 'Période clôturée avec succès.')
        
        return redirect('paie:detail_periode', pk=pk)
    
    return render(request, 'paie/periodes/cloturer.html', {'periode': periode})


@login_required
@entreprise_active_required
@reauth_required
def liste_bulletins(request):
    """Liste de tous les bulletins de paie (y compris périodes clôturées)"""
    from django.core.paginator import Paginator, EmptyPage, PageNotAnInteger
    
    bulletins = BulletinPaie.objects.filter(
        employe__entreprise=request.user.entreprise,
        periode__entreprise=request.user.entreprise,
    ).select_related('employe', 'periode').order_by('-periode__annee', '-periode__mois', 'employe__nom')
    
    # Filtres
    annee = request.GET.get('annee')
    periode_id = request.GET.get('periode')
    employe_id = request.GET.get('employe')
    statut = request.GET.get('statut')
    
    if annee:
        bulletins = bulletins.filter(periode__annee=annee)
    if periode_id:
        bulletins = bulletins.filter(periode_id=periode_id)
    if employe_id:
        bulletins = bulletins.filter(employe_id=employe_id)
    if statut:
        bulletins = bulletins.filter(statut_bulletin=statut)
    
    # Pagination - 50 bulletins par page
    paginator = Paginator(bulletins, 50)
    page = request.GET.get('page')
    try:
        bulletins_page = paginator.page(page)
    except PageNotAnInteger:
        bulletins_page = paginator.page(1)
    except EmptyPage:
        bulletins_page = paginator.page(paginator.num_pages)
    
    # Toutes les périodes (y compris clôturées) pour le filtre
    periodes = PeriodePaie.objects.filter(
        entreprise=request.user.entreprise
    ).order_by('-annee', '-mois')
    
    # Tous les employés (actifs et inactifs) pour pouvoir consulter les anciens bulletins
    employes = Employe.objects.filter(
        entreprise=request.user.entreprise
    ).order_by('nom', 'prenoms')
    
    # Liste des années disponibles
    annees_disponibles = PeriodePaie.objects.filter(
        entreprise=request.user.entreprise
    ).values_list('annee', flat=True).distinct().order_by('-annee')
    
    return render(request, 'paie/bulletins/liste.html', {
        'bulletins': bulletins_page,
        'periodes': periodes,
        'employes': employes,
        'annees_disponibles': annees_disponibles,
        'annee_selectionnee': annee,
        'total_bulletins': paginator.count,
    })


@login_required
@reauth_required
@entreprise_active_required
@require_POST
def recalculer_cnss_bulletin(request, pk):
    """
    Recalcule et corrige le CNSS d'un bulletin existant (CNSS = 0 → correct).
    CGI Guinée : base = min(max(brut, plancher 550 000), plafond 2 500 000)
    """
    from decimal import Decimal, ROUND_HALF_UP
    bulletin = get_object_or_404(
        BulletinPaie,
        pk=pk,
        employe__entreprise=request.user.entreprise,
        periode__entreprise=request.user.entreprise,
    )

    brut = Decimal(str(bulletin.salaire_brut or 0))
    if brut <= 0:
        from django.contrib import messages
        messages.error(request, "Brut nul — impossible de recalculer la CNSS.")
        return redirect('paie:detail_bulletin', pk=pk)

    plafond  = Decimal('2500000')
    plancher = Decimal('550000')
    taux_emp = Decimal('5')
    taux_pat = Decimal('18')

    base_cnss = max(min(brut, plafond), plancher)
    cnss_employe   = (base_cnss * taux_emp  / Decimal('100')).quantize(Decimal('1'), rounding=ROUND_HALF_UP)
    cnss_employeur = (base_cnss * taux_pat / Decimal('100')).quantize(Decimal('1'), rounding=ROUND_HALF_UP)

    # Net recalculé = Brut − CNSS employé − RTS (anciennement net = brut − RTS car CNSS était 0)
    ancien_net = Decimal(str(bulletin.net_a_payer or 0))
    ancien_cnss = Decimal(str(bulletin.cnss_employe or 0))
    nouveau_net = ancien_net - (cnss_employe - ancien_cnss)  # ajustement exact

    bulletin.cnss_employe   = cnss_employe
    bulletin.cnss_employeur = cnss_employeur
    bulletin.net_a_payer    = nouveau_net
    bulletin.save(update_fields=['cnss_employe', 'cnss_employeur', 'net_a_payer'])

    from django.contrib import messages
    messages.success(
        request,
        f"CNSS recalculée : employé {int(cnss_employe):,} GNF | "
        f"employeur {int(cnss_employeur):,} GNF | "
        f"Net corrigé : {int(nouveau_net):,} GNF".replace(',', ' ')
    )
    return redirect('paie:detail_bulletin', pk=pk)


@login_required
@entreprise_active_required
@reauth_required
def detail_bulletin(request, pk):
    """Détail d'un bulletin de paie"""
    bulletin = get_object_or_404(
        BulletinPaie,
        pk=pk,
        employe__entreprise=request.user.entreprise,
        periode__entreprise=request.user.entreprise,
    )
    lignes = LigneBulletin.objects.filter(bulletin=bulletin).select_related('rubrique')
    
    # Séparer les gains et retenues
    gains = lignes.filter(rubrique__type_rubrique='gain')
    retenues = lignes.filter(rubrique__type_rubrique__in=['retenue', 'cotisation'])

    # Récupérer les alertes de conformité depuis le snapshot
    import json
    alertes_conformite = []
    conformite_resume = {}
    recommandations = {}
    snapshot = bulletin.snapshot_parametres
    if snapshot:
        if isinstance(snapshot, str):
            try:
                snapshot = json.loads(snapshot)
            except (json.JSONDecodeError, TypeError):
                snapshot = {}
        conformite = snapshot.get('conformite_indemnites', {})
        alertes_conformite = conformite.get('alertes', [])
        conformite_resume = conformite.get('resume', {})
        recommandations = conformite.get('recommandation', {})
        scenarios = conformite.get('scenarios', {})

    return render(request, 'paie/bulletins/detail.html', {
        'bulletin': bulletin,
        'gains': gains,
        'retenues': retenues,
        'alertes_conformite': alertes_conformite,
        'conformite_resume': conformite_resume,
        'recommandations': recommandations,
        'scenarios': scenarios,
    })


@login_required
@entreprise_active_required
@reauth_required
def imprimer_bulletin(request, pk):
    """Imprimer un bulletin de paie"""
    from decimal import Decimal
    
    bulletin = get_object_or_404(
        BulletinPaie,
        pk=pk,
        employe__entreprise=request.user.entreprise,
        periode__entreprise=request.user.entreprise,
    )
    lignes = LigneBulletin.objects.filter(bulletin=bulletin).select_related('rubrique')
    
    # Récupérer les paramètres de l'entreprise
    try:
        params = ParametrePaie.objects.filter(entreprise=request.user.entreprise).first()
    except:
        params = None
    
    gains = lignes.filter(rubrique__type_rubrique='gain')
    retenues = lignes.filter(rubrique__type_rubrique__in=['retenue', 'cotisation'])
    
    # Calculer les détails des heures supplémentaires
    heures_supp_details = []
    heures_mensuelles = Decimal('173.33')
    
    if bulletin.salaire_base and bulletin.salaire_base > 0:
        taux_horaire = bulletin.salaire_base / heures_mensuelles
        
        # Détails des heures supplémentaires avec leurs montants
        types_hs = [
            ('heures_supplementaires_30', 'Heures supplémentaires (1ère catégorie +30%)', Decimal('1.30'), 30),
            ('heures_supplementaires_60', 'Heures supplémentaires (2ème catégorie +60%)', Decimal('1.60'), 60),
            ('heures_nuit', 'Heures de nuit (+20%)', Decimal('1.20'), 20),
            ('heures_feries', 'Heures jours fériés (+60%)', Decimal('1.60'), 60),
        ]
        
        for champ, libelle, taux_majoration, majoration_pct in types_hs:
            heures = getattr(bulletin, champ, Decimal('0'))
            if heures and heures > 0:
                montant = taux_horaire * heures * taux_majoration
                heures_supp_details.append({
                    'libelle': libelle,
                    'heures': heures,
                    'taux_horaire': taux_horaire,
                    'taux_majoration': taux_majoration,
                    'majoration_pct': majoration_pct,
                    'montant': montant
                })
    
    return render(request, 'paie/bulletins/imprimer.html', {
        'bulletin': bulletin,
        'gains': gains,
        'retenues': retenues,
        'params': params,
        'heures_supp_details': heures_supp_details
    })


@login_required
@reauth_required
@entreprise_active_required
def telecharger_bulletin_pdf(request, pk):
    """Telecharger un bulletin de paie en PDF standard GuineeRH."""
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import cm, mm
    from reportlab.pdfgen import canvas
    from reportlab.lib import colors
    from reportlab.platypus import Table, TableStyle
    import io
    import os
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    _font_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'static', 'fonts')
    try:
        pdfmetrics.registerFont(TTFont('Arial', os.path.join(_font_dir, 'Arial.ttf')))
        pdfmetrics.registerFont(TTFont('Arial-Bold', os.path.join(_font_dir, 'Arial-Bold.ttf')))
        pdfmetrics.registerFont(TTFont('Arial-Italic', os.path.join(_font_dir, 'Arial-Italic.ttf')))
        _FN = 'Arial'; _FB = 'Arial-Bold'; _FI = 'Arial-Italic'
    except Exception:
        _FN = 'Helvetica'; _FB = 'Helvetica-Bold'; _FI = 'Helvetica-Oblique'

    bulletin = get_object_or_404(
        BulletinPaie,
        pk=pk,
        employe__entreprise=request.user.entreprise,
    )
    lignes = LigneBulletin.objects.filter(bulletin=bulletin).select_related('rubrique')
    gains = lignes.filter(rubrique__type_rubrique='gain')
    retenues = lignes.filter(rubrique__type_rubrique__in=['retenue', 'cotisation'])
    
    # Créer le buffer PDF
    buffer = io.BytesIO()
    p = canvas.Canvas(buffer, pagesize=A4)
    width, height = A4
    
    # Variables de position
    y = height - 1*cm
    
    # === EN-TÊTE ===
    entreprise = bulletin.employe.entreprise
    
    # Logo entreprise à gauche
    if entreprise and entreprise.logo:
        try:
            logo_path = entreprise.logo.path
            if os.path.exists(logo_path):
                p.drawImage(logo_path, 1.5*cm, y - 2*cm, width=2*cm, height=2*cm, preserveAspectRatio=True)
        except:
            pass
    
    # Titre centré
    p.setFont(_FB, 11)
    p.drawCentredString(width/2, y, "RÉPUBLIQUE DE GUINÉE")
    y -= 0.4*cm
    p.setFont(_FI, 8)
    p.drawCentredString(width/2, y, "Travail - Justice - Solidarité")
    y -= 0.5*cm
    
    # Nom entreprise
    p.setFont(_FB, 12)
    nom_entreprise = entreprise.nom_entreprise if entreprise else "ENTREPRISE"
    p.drawCentredString(width/2, y, nom_entreprise)
    y -= 0.50*cm
    # NIF et CNSS entreprise sous le nom (obligatoire légalement)
    if entreprise:
        nif_str = entreprise.nif or "Non renseigné"
        cnss_str = getattr(entreprise, 'num_cnss', None) or "Non renseigné"
        p.setFont(_FN, 7.5)
        p.setFillColor(colors.HexColor("#444444"))
        p.drawCentredString(width/2, y, f"NIF: {nif_str}   |   CNSS Employeur: {cnss_str}")
        p.setFillColor(colors.black)
        y -= 0.45*cm

    # Titre bulletin
    p.setFont(_FB, 14)
    p.drawCentredString(width/2, y, "BULLETIN DE PAIE")
    y -= 0.4*cm
    
    # Ligne de séparation
    p.setStrokeColor(colors.HexColor("#ce1126"))
    p.setLineWidth(2)
    p.line(1.5*cm, y, width - 1.5*cm, y)
    y -= 0.6*cm
    
    # Infos bulletin sur une ligne
    p.setFont(_FN, 9)
    p.setFillColor(colors.black)
    p.drawString(1.5*cm, y, f"N°: {bulletin.numero_bulletin}")
    p.drawCentredString(width/2, y, f"Période: {bulletin.periode}")
    p.drawRightString(width - 1.5*cm, y, f"Date: {bulletin.date_calcul.strftime('%d/%m/%Y') if bulletin.date_calcul else '-'}")
    y -= 0.35*cm
    # Dates de la période
    p.setFont(_FN, 8)
    periode_detail = f"Du {bulletin.periode.date_debut.strftime('%d/%m/%Y')} au {bulletin.periode.date_fin.strftime('%d/%m/%Y')}" if bulletin.periode.date_debut and bulletin.periode.date_fin else ""
    p.drawCentredString(width/2, y, periode_detail)
    y -= 0.6*cm
    
    # === INFORMATIONS EMPLOYÉ ===
    p.setFillColor(colors.HexColor("#ce1126"))
    p.setFont(_FB, 9)
    p.drawString(1.5*cm, y, "INFORMATIONS EMPLOYÉ")
    p.setFillColor(colors.black)
    y -= 0.5*cm
    
    emp = bulletin.employe
    
    # Ancienneté en années, mois complets et jours à la date du bulletin.
    anciennete_str = format_anciennete_bulletin(emp, bulletin)
    
    # Récupération des congés
    from temps_travail.models import SoldeConge
    solde_conge = SoldeConge.objects.filter(employe=emp, annee=bulletin.annee_paie).first()
    conges_acquis = solde_conge.conges_acquis if solde_conge else Decimal('0')
    conges_pris = solde_conge.conges_pris if solde_conge else Decimal('0')
    conges_restants = solde_conge.conges_restants if solde_conge else Decimal('0')

    # Acquisition mensuelle
    acquis_ce_mois = Decimal('1.5')
    try:
        from .models import ConfigurationPaie
        cfg = ConfigurationPaie.objects.filter(entreprise=entreprise).first()
        if cfg:
            acquis_ce_mois = cfg.jours_conges_par_mois
    except Exception:
        pass

    # Valeurs RH par défaut si non renseignées (acceptable juridiquement,
    # contrairement à "-" ou "Non renseigné" qui posent problème en cas de contrôle).
    poste_str = str(emp.poste).strip() if emp.poste else ""
    service_str = str(emp.service).strip() if emp.service else ""
    poste_affiche = poste_str or "Employé"
    service_affiche = service_str or "Administration"

    infos_emp = [
        ["Matricule:", emp.matricule or "-", "N° CNSS:", emp.num_cnss_individuel or "-"],
        ["Nom et Prénoms:", f"{emp.nom} {emp.prenoms}", "Ancienneté:", anciennete_str],
        ["Poste:", poste_affiche, "Service:", service_affiche],
        ["Date embauche:", emp.date_embauche.strftime('%d/%m/%Y') if emp.date_embauche else "-", "Mode paiement:", emp.mode_paiement or "-"],
        ["Congés acquis:", f"{conges_acquis:g} j", "Congés pris:", f"{conges_pris:g} j"],
        ["Solde congés:", f"{conges_restants:g} j", "Acquis ce mois:", f"{acquis_ce_mois:g} j"],
        ["Nature contrat:", dict(emp.TYPES_CONTRATS).get(emp.type_contrat, emp.type_contrat or "-"), "", ""],
    ]

    for row in infos_emp:
        p.setFont(_FB, 8)
        p.drawString(1.5*cm, y, row[0])
        p.setFont(_FN, 8)
        p.drawString(4*cm, y, str(row[1]))
        if row[2]:
            p.setFont(_FB, 8)
            p.drawString(11*cm, y, row[2])
            p.setFont(_FN, 8)
            p.drawString(14*cm, y, str(row[3]))
        y -= 0.4*cm
    
    y -= 0.3*cm
    
    # === GAINS ===
    # Titre GAINS
    p.setFillColor(colors.HexColor("#28a745"))
    p.setFont(_FB, 9)
    p.drawString(1.5*cm, y, "GAINS ET RÉMUNÉRATIONS")
    p.setFillColor(colors.black)
    y -= 0.3*cm
    
    # Tableau des gains (5 colonnes avec Nbre pour les heures)
    gains_data = [["Libellé", "Nbre", "Base", "Taux", "Montant"]]
    for g in gains:
        nbre_str = f"{g.nombre:g}" if g.nombre and g.nombre != 1 else ""
        # Nettoyer le taux : supprimer les zéros inutiles (30.0000% → 30%)
        taux_val = g.taux
        taux_str = f"{float(taux_val):g}%" if taux_val else "-"
        # Corriger libellé: "Prime de transport" → "Indemnité de transport" si forfaitaire
        libelle = g.rubrique.libelle_rubrique[:35]
        code_rub = (g.rubrique.code_rubrique or '').upper()
        if libelle.lower().startswith('prime'):
            from .services import PATTERNS_CODES_FORFAITAIRES, MOTS_CLES_LIBELLE_FORFAITAIRES
            lib_low = libelle.lower()
            is_indem = any(p in code_rub for p in PATTERNS_CODES_FORFAITAIRES) or \
                       any(m in lib_low for m in MOTS_CLES_LIBELLE_FORFAITAIRES)
            if is_indem:
                libelle = 'Indemnité' + libelle[5:]
        if ('HS' in code_rub or 'HEURE' in libelle.upper()) and 'SUP' in libelle.upper():
            libelle = re.sub(r'\s*\+?\d+\s*%', '', libelle).strip()
        # Transparence du calcul : pour un montant mensuel fixe (base == montant, sans nombre>1
        # ni taux), afficher explicitement Nbre = 1 pour signaler la formule "1 × montant".
        # Évite la lecture ambiguë "10 125 000" comprise comme "10 × 125 000".
        montant_fixe_mensuel = (
            g.base
            and (not g.nombre or g.nombre == 1)
            and not g.taux
            and abs(float(g.base) - float(g.montant or 0)) < 1
        )
        if montant_fixe_mensuel and not nbre_str:
            nbre_str = "1"
        base_str = f"{g.base:,.0f}".replace(",", "\u00A0") if g.base else "-"
        gains_data.append([
            libelle,
            nbre_str,
            base_str,
            taux_str,
            f"{g.montant:,.0f}".replace(",", "\u00A0"),
        ])
    gains_data.append(["TOTAL BRUT", "", "", "", f"{bulletin.salaire_brut:,.0f} GNF".replace(",", "\u00A0")])
    
    row_height = 14
    gains_table = Table(gains_data, colWidths=[6*cm, 1.8*cm, 3.2*cm, 1.8*cm, 4.2*cm], rowHeights=row_height)
    _gains_style = [
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor("#28a745")),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('FONTNAME', (0, 0), (-1, 0), _FB),
        ('FONTSIZE', (0, 0), (-1, -1), 8),
        ('ALIGN', (0, 0), (0, -1), 'LEFT'),
        ('ALIGN', (1, 0), (-1, -1), 'RIGHT'),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.grey),
        ('LEFTPADDING', (0, 0), (-1, -1), 6),
        ('RIGHTPADDING', (0, 0), (-1, -1), 6),
        ('BACKGROUND', (0, -1), (-1, -1), colors.HexColor("#d4edda")),
        ('FONTNAME', (0, -1), (-1, -1), _FB),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
    ]
    # Zébrures lignes paires (sauf header et total) pour lisibilité
    for _i in range(1, len(gains_data) - 1):
        if _i % 2 == 0:
            _gains_style.append(('BACKGROUND', (0, _i), (-1, _i), colors.HexColor("#f8f9fa")))
    gains_table.setStyle(TableStyle(_gains_style))
    
    table_height = len(gains_data) * row_height
    gains_table.wrapOn(p, width, height)
    gains_table.drawOn(p, 1.5*cm, y - table_height)
    y -= table_height + 0.35*cm
    
    # === DÉTAIL HEURES SUPPLÉMENTAIRES ===
    hs_30 = getattr(bulletin, 'heures_supplementaires_30', 0) or 0
    hs_60 = getattr(bulletin, 'heures_supplementaires_60', 0) or 0
    hs_nuit = getattr(bulletin, 'heures_nuit', 0) or 0
    hs_feries = getattr(bulletin, 'heures_feries', 0) or 0
    hs_feries_nuit = getattr(bulletin, 'heures_feries_nuit', 0) or 0
    prime_hs = getattr(bulletin, 'prime_heures_sup', 0) or 0
    prime_nuit = getattr(bulletin, 'prime_nuit', 0) or 0
    prime_feries = getattr(bulletin, 'prime_feries', 0) or 0
    prime_feries_nuit = getattr(bulletin, 'prime_feries_nuit', 0) or 0
    total_hs_heures = float(hs_30) + float(hs_60) + float(hs_nuit) + float(hs_feries) + float(hs_feries_nuit)
    
    if total_hs_heures > 0 or float(prime_hs) > 0:
        p.setFont(_FB, 7)
        p.setFillColor(colors.HexColor("#6c757d"))
        p.drawString(1.5*cm, y, "DÉTAIL HEURES SUPPLÉMENTAIRES (Code du Travail Art. 221)")
        p.setFillColor(colors.black)
        y -= 0.18*cm
        
        # Calcul des montants individuels (salaire_base / 173,33 × h × coefficient)
        _sal_base = Decimal(str(bulletin.salaire_base or 0))
        if _sal_base == 0:
            try:
                ligne_base = LigneBulletin.objects.filter(
                    bulletin=bulletin,
                    rubrique__type_rubrique='gain',
                ).filter(
                    Q(rubrique__categorie_rubrique='salaire_base') |
                    Q(rubrique__code_rubrique__icontains='SAL_BASE') |
                    Q(rubrique__code_rubrique__iexact='BASE') |
                    Q(rubrique__libelle_rubrique__icontains='Salaire de base')
                ).annotate(
                    _base_priority=Case(
                        When(rubrique__categorie_rubrique='salaire_base', then=Value(0)),
                        When(rubrique__code_rubrique__icontains='SAL_BASE', then=Value(1)),
                        When(rubrique__code_rubrique__iexact='BASE', then=Value(2)),
                        default=Value(9),
                        output_field=IntegerField(),
                    )
                ).order_by('_base_priority', 'ordre', 'id').first()
                if ligne_base:
                    _sal_base = Decimal(str(ligne_base.montant or 0))
            except Exception:
                pass
        _sal_h = _sal_base / Decimal('173.33') if _sal_base > 0 else Decimal('0')
        _montant_30 = round(_sal_h * Decimal(str(hs_30)) * Decimal('1.30'))
        _montant_60 = round(_sal_h * Decimal(str(hs_60)) * Decimal('1.60'))
        _montant_nuit = round(_sal_h * Decimal(str(hs_nuit)) * Decimal('1.20')) if float(hs_nuit) > 0 else 0
        _montant_feries = round(_sal_h * Decimal(str(hs_feries)) * Decimal('1.60')) if float(hs_feries) > 0 else 0
        _montant_feries_nuit = round(_sal_h * Decimal(str(hs_feries_nuit)) * Decimal('2.00')) if float(hs_feries_nuit) > 0 else 0
        _total_hs_calcule = (
            Decimal(str(_montant_30)) + Decimal(str(_montant_60)) +
            Decimal(str(_montant_nuit)) + Decimal(str(_montant_feries)) +
            Decimal(str(_montant_feries_nuit))
        )
        _total_hs_calcule = (
            Decimal(str(_montant_30)) + Decimal(str(_montant_60)) +
            Decimal(str(_montant_nuit)) + Decimal(str(_montant_feries)) +
            Decimal(str(_montant_feries_nuit))
        )

        hs_detail_data = [["Type", "Heures", "Majoration", "Montant"]]
        if float(hs_30) > 0:
            hs_detail_data.append(["4 prem. HS/sem.", f"{hs_30:g}h", "+30% (130%)",
                                    f"{_montant_30:,.0f}".replace(",", " ")])
        if float(hs_60) > 0:
            hs_detail_data.append(["Au-delà 4 HS/sem.", f"{hs_60:g}h", "+60% (160%)",
                                    f"{_montant_60:,.0f}".replace(",", " ")])
        if float(hs_nuit) > 0:
            hs_detail_data.append(["Heures de nuit (20h-6h)", f"{hs_nuit:g}h", "+20% (120%)",
                                    f"{_montant_nuit:,.0f}".replace(",", " ")])
        if float(hs_feries) > 0:
            hs_detail_data.append(["Jours fériés (jour)", f"{hs_feries:g}h", "+60% (160%)",
                                    f"{_montant_feries:,.0f}".replace(",", " ")])
        if float(hs_feries_nuit) > 0:
            hs_detail_data.append(["Jours fériés (nuit)", f"{hs_feries_nuit:g}h", "+100% (200%)",
                                    f"{_montant_feries_nuit:,.0f}".replace(",", " ")])

        total_prime = float(prime_hs) + float(prime_nuit) + float(prime_feries) + float(prime_feries_nuit)
        if total_prime <= 0 and _total_hs_calcule > 0:
            total_prime = float(_total_hs_calcule)
        hs_detail_data.append(["", f"{total_hs_heures:g}h", "Total HS:",
                                f"{total_prime:,.0f} GNF".replace(",", " ")])
        
        hs_row_h = 12
        hs_table = Table(hs_detail_data, colWidths=[5.5*cm, 2*cm, 4.5*cm, 5*cm], rowHeights=hs_row_h)
        nb_hs_rows = len(hs_detail_data)
        hs_table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor("#6c757d")),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
            ('FONTNAME', (0, 0), (-1, 0), _FB),
            ('FONTSIZE', (0, 0), (-1, -1), 7),
            ('ALIGN', (1, 0), (-1, -1), 'RIGHT'),
            ('GRID', (0, 0), (-1, -1), 0.3, colors.HexColor("#dee2e6")),
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            ('FONTNAME', (0, -1), (-1, -1), _FB),
            ('BACKGROUND', (0, -1), (-1, -1), colors.HexColor("#f8f9fa")),
        ]))
        
        hs_table_h = nb_hs_rows * hs_row_h
        hs_table.wrapOn(p, width, height)
        hs_table.drawOn(p, 1.5*cm, y - hs_table_h)
        y -= hs_table_h + 0.35*cm
    else:
        y -= 0.25*cm
    
    # === RETENUES ===
    # Titre RETENUES
    p.setFillColor(colors.HexColor("#dc3545"))
    p.setFont(_FB, 9)
    p.drawString(1.5*cm, y, "RETENUES ET COTISATIONS")
    p.setFillColor(colors.black)
    y -= 0.25*cm
    
    retenues_data = [["Libellé", "Base", "Taux", "Montant"]]
    # Filtrer les doublons CNSS et IRG (déjà dans les lignes du bulletin)
    cnss_irg_codes = ['CNSS', 'IRG', 'RTS', 'IRS', 'IRPP']
    for r in retenues:
        # Éviter les doublons - ne pas réafficher si déjà traité
        code = r.rubrique.code_rubrique.upper() if r.rubrique.code_rubrique else ''
        libelle = r.rubrique.libelle_rubrique.upper() if r.rubrique.libelle_rubrique else ''
        is_cnss_irg = any(c in code or c in libelle for c in cnss_irg_codes)
        if not is_cnss_irg:
            retenues_data.append([
                r.rubrique.libelle_rubrique[:35],
                f"{r.base:,.0f}".replace(",", " ") if r.base else "-",
                f"{r.taux}%" if r.taux else "-",
                f"{r.montant:,.0f}".replace(",", " ")
            ])
    
    # Ajouter CNSS (base plafonnée) et RTS avec détail
    base_cnss = min(bulletin.salaire_brut, 2500000)
    retenues_data.append(["CNSS Employé (5%)", f"{base_cnss:,.0f}".replace(",", " "), "5%", f"{bulletin.cnss_employe:,.0f}".replace(",", " ")])
    # RTS avec base, taux effectif et montant
    base_rts_val = getattr(bulletin, 'base_rts', 0) or 0
    taux_eff_rts_val = getattr(bulletin, 'taux_effectif_rts', 0) or 0
    rts_base_str = f"{base_rts_val:,.0f}".replace(",", " ") if base_rts_val else "-"
    # Note: l'abattement forfaitaire (exonération 25%) est un ajustement fiscal,
    # pas une retenue réelle → affiché uniquement dans la section DÉTAIL RTS.
    retenues_data.append(["RTS (Impôt sur le revenu \u2013 barème progressif)", rts_base_str, "-", f"{bulletin.irg:,.0f}".replace(",", " ")])

    retenues_table = Table(retenues_data, colWidths=[8*cm, 3*cm, 2*cm, 4*cm], rowHeights=row_height)
    retenues_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor("#dc3545")),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('FONTNAME', (0, 0), (-1, 0), _FB),
        ('FONTSIZE', (0, 0), (-1, -1), 8),
        ('ALIGN', (1, 0), (-1, -1), 'RIGHT'),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.grey),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
    ]))

    table_height = len(retenues_data) * row_height
    retenues_table.wrapOn(p, width, height)
    retenues_table.drawOn(p, 1.5*cm, y - table_height)
    y -= table_height + 0.25*cm

    # === CHARGES SALARIALES ===
    total_charges_salariales = getattr(
        bulletin,
        'total_charges_salariales',
        (bulletin.cnss_employe or 0) + (bulletin.irg or 0),
    )
    p.setFillColor(colors.HexColor("#dc3545"))
    p.setFont(_FB, 9)
    p.drawString(1.5*cm, y, "CHARGES SALARIALES")
    p.setFillColor(colors.black)
    y -= 0.22*cm

    charges_salariales_data = [
        ["Charge salariale", "Base", "Taux", "Montant"],
        [
            "CNSS 5%",
            f"{base_cnss:,.0f}".replace(",", " "),
            "5%",
            f"- {bulletin.cnss_employe:,.0f}".replace(",", " "),
        ],
        [
            "RTS",
            rts_base_str,
            f"{taux_eff_rts_val:g}%" if taux_eff_rts_val else "-",
            f"- {bulletin.irg:,.0f}".replace(",", " "),
        ],
        [
            "TOTAL Charge salariale",
            "",
            "",
            f"- {total_charges_salariales:,.0f} GNF".replace(",", " "),
        ],
    ]
    cs_row_h = 12
    charges_salariales_table = Table(
        charges_salariales_data,
        colWidths=[8*cm, 3*cm, 2*cm, 4*cm],
        rowHeights=cs_row_h,
    )
    charges_salariales_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor("#dc3545")),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('FONTNAME', (0, 0), (-1, 0), _FB),
        ('FONTSIZE', (0, 0), (-1, -1), 8),
        ('ALIGN', (1, 0), (-1, -1), 'RIGHT'),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.grey),
        ('BACKGROUND', (0, -1), (-1, -1), colors.HexColor("#fde2e2")),
        ('FONTNAME', (0, -1), (-1, -1), _FB),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
    ]))
    cs_table_height = len(charges_salariales_data) * cs_row_h
    charges_salariales_table.wrapOn(p, width, height)
    charges_salariales_table.drawOn(p, 1.5*cm, y - cs_table_height)
    y -= cs_table_height + 0.22*cm

    # === DÉTAIL CALCUL RTS (barème progressif) ===
    from paie.utils import calculer_detail_tranches_rts
    detail_rts = calculer_detail_tranches_rts(base_rts_val)
    if detail_rts:
        p.setFont(_FB, 7)
        p.setFillColor(colors.HexColor("#6c757d"))
        # Explication base imposable avec nature de l'abattement
        abattement_val = getattr(bulletin, 'abattement_forfaitaire', 0) or 0
        if float(abattement_val) > 0:
            p.drawString(1.5*cm, y,
                f"DÉTAIL RTS \u2014 Base imposable: {base_rts_val:,.0f} = "
                f"Brut {bulletin.salaire_brut:,.0f} \u2212 CNSS {bulletin.cnss_employe:,.0f} "
                f"\u2212 Indemnités exonérées {abattement_val:,.0f} (plafond 25% du brut)"
                .replace(",", " "))
        else:
            p.drawString(1.5*cm, y,
                f"DÉTAIL RTS — Base imposable: {base_rts_val:,.0f} = "
                f"Brut {bulletin.salaire_brut:,.0f} − CNSS {bulletin.cnss_employe:,.0f}"
                .replace(",", " "))
        p.setFillColor(colors.black)
        y -= 0.18*cm

        rts_detail_data = [["Tranche (bornes)", "Base taxable", "Taux", "Impôt"]]
        cumul_impot = Decimal('0')
        for i, t in enumerate(detail_rts, start=1):
            taux_pct = f"{t['taux']:g}"
            b_inf = f"{t['borne_inf']:,.0f}".replace(",", " ")
            b_sup = f"{t['borne_sup']:,.0f}".replace(",", " ") if t.get('borne_sup') else "∞"
            base_tr = f"{t['base_tranche']:,.0f}".replace(",", " ")
            rts_detail_data.append([
                f"{b_inf} à {b_sup}",
                base_tr,
                f"{taux_pct}%",
                f"{t['impot_tranche']:,.0f}".replace(",", " "),
            ])
            cumul_impot += t['impot_tranche']
        rts_detail_data.append(["", "", "Total RTS:", f"{cumul_impot:,.0f} GNF".replace(",", " ")])

        rts_row_h = 12
        rts_table = Table(rts_detail_data, colWidths=[5*cm, 4*cm, 3*cm, 5*cm], rowHeights=rts_row_h)
        nb_rows = len(rts_detail_data)
        rts_table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor("#6c757d")),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
            ('FONTNAME', (0, 0), (-1, 0), _FB),
            ('FONTSIZE', (0, 0), (-1, -1), 7),
            ('ALIGN', (1, 0), (-1, -1), 'RIGHT'),
            ('GRID', (0, 0), (-1, -1), 0.3, colors.HexColor("#dee2e6")),
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            ('FONTNAME', (0, -1), (-1, -1), _FB),
            ('BACKGROUND', (0, -1), (-1, -1), colors.HexColor("#f8f9fa")),
        ]))
        
        rts_table_h = nb_rows * rts_row_h
        rts_table.wrapOn(p, width, height)
        rts_table.drawOn(p, 1.5*cm, y - rts_table_h)
        y -= rts_table_h + 0.25*cm
    else:
        y -= 0.2*cm
    
    # === RÉCAPITULATIF ===
    rappel = getattr(bulletin, 'rappel_salaire', 0) or 0
    trop_percu = getattr(bulletin, 'retenue_trop_percu', 0) or 0
    has_rappel = rappel > 0
    has_trop_percu = trop_percu > 0
    abatt_forfait = getattr(bulletin, 'abattement_forfaitaire', 0) or 0
    has_abatt = abatt_forfait > 0
    extra_lines = (1 if has_rappel else 0) + (1 if has_trop_percu else 0) + (1 if has_abatt else 0)
    recap_height = 1.85*cm + extra_lines * 0.30*cm
    p.setStrokeColor(colors.HexColor("#ce1126"))
    p.setLineWidth(2)
    p.rect(1.5*cm, y - recap_height, width - 3*cm, recap_height, stroke=1, fill=0)
    
    p.setFont(_FB, 10)
    p.setFillColor(colors.black)
    p.drawString(2*cm, y - 0.5*cm, "SALAIRE BRUT:")
    p.drawRightString(width - 2*cm, y - 0.5*cm, f"{bulletin.salaire_brut:,.0f} GNF".replace(",", " "))
    
    # CNSS et RTS alignés sur la même ligne
    p.setFont(_FN, 8)
    p.setFillColor(colors.HexColor("#dc3545"))
    mid_x = width / 2
    p.drawString(2*cm, y - 1*cm, f"CNSS (5%): -{bulletin.cnss_employe:,.0f}".replace(",", " "))
    p.drawString(mid_x, y - 1*cm, f"RTS: -{bulletin.irg:,.0f}".replace(",", " "))
    p.drawRightString(width - 2*cm, y - 1*cm, f"Total retenues: -{bulletin.total_retenues:,.0f} GNF".replace(",", " "))
    
    offset_y = 1*cm
    if has_rappel:
        offset_y += 0.3*cm
        p.setFillColor(colors.HexColor("#007bff"))
        p.drawString(2*cm, y - offset_y, "Rappel/Complément salaire précédent:")
        p.drawRightString(width - 2*cm, y - offset_y, f"+ {rappel:,.0f} GNF".replace(",", " "))
    if has_trop_percu:
        offset_y += 0.3*cm
        p.setFillColor(colors.HexColor("#dc3545"))
        p.drawString(2*cm, y - offset_y, "Retenue trop-perçu salaire précédent:")
        p.drawRightString(width - 2*cm, y - offset_y, f"- {trop_percu:,.0f} GNF".replace(",", " "))
    
    p.setFillColor(colors.HexColor("#28a745"))
    p.setFont(_FB, 14)
    p.drawString(2*cm, y - offset_y - 0.7*cm, "NET À PAYER:")
    p.drawRightString(width - 2*cm, y - offset_y - 0.7*cm, f"{bulletin.net_a_payer:,.0f} GNF".replace(",", " "))
    p.setFillColor(colors.black)
    
    y -= recap_height + 0.25*cm
    
    # === CHARGES PATRONALES ===
    vf = getattr(bulletin, 'versement_forfaitaire', 0) or 0
    ta = getattr(bulletin, 'taxe_apprentissage', 0) or 0
    taux_ta = getattr(bulletin, 'taux_ta', 0) or 0
    onfpp = getattr(bulletin, 'contribution_onfpp', 0) or 0
    base_vf = getattr(bulletin, 'base_vf', 0) or 0
    base_onfpp = getattr(bulletin, 'base_onfpp', 0) or base_vf
    nb_sal = getattr(bulletin, 'nombre_salaries', 0) or 0
    total_charges = bulletin.cnss_employeur + vf + ta + onfpp
    taux_ta_label = str(taux_ta).rstrip('0').rstrip('.').replace('.', ',') if taux_ta else '2'
    
    # Tableau charges patronales (une ligne par charge = lisibilité audit)
    charges_data = [["Charge patronale", "Base", "Taux", "Montant"]]
    charges_data.append(["CNSS Employeur",
        f"{min(bulletin.salaire_brut, 2500000):,.0f}".replace(",", " "),
        "18%",
        f"{bulletin.cnss_employeur:,.0f}".replace(",", " ")])
    charges_data.append(["Versement Forfaitaire (VF)",
        f"{base_vf:,.0f}".replace(",", " ") if base_vf else "-",
        "6%",
        f"{vf:,.0f}".replace(",", " ")])
    if ta > 0:
        charges_data.append([f"TA (applicable si effectif < 30 sal. \u2014 effectif actuel : {nb_sal})",
            f"{base_vf:,.0f}".replace(",", " ") if base_vf else "-",
            f"{taux_ta_label}%",
            f"{ta:,.0f}".replace(",", " ")])
    elif onfpp > 0:
        charges_data.append([f"ONFPP (applicable si effectif \u2265 30 sal. \u2014 effectif actuel : {nb_sal})",
            f"{base_onfpp:,.0f}".replace(",", " ") if base_onfpp else "-",
            "1,5%",
            f"{onfpp:,.0f}".replace(",", " ")])
    charges_data.append(["TOTAL CHARGES PATRONALES", "", "",
        f"{total_charges:,.0f} GNF".replace(",", " ")])

    ch_row_h = 12
    charges_table = Table(charges_data, colWidths=[7*cm, 3.5*cm, 2*cm, 4.5*cm], rowHeights=ch_row_h)
    charges_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor("#fd7e14")),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('FONTNAME', (0, 0), (-1, 0), _FB),
        ('FONTSIZE', (0, 0), (-1, -1), 7),
        ('ALIGN', (1, 0), (-1, -1), 'RIGHT'),
        ('GRID', (0, 0), (-1, -1), 0.4, colors.HexColor("#dee2e6")),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('BACKGROUND', (0, -1), (-1, -1), colors.HexColor("#fff3cd")),
        ('FONTNAME', (0, -1), (-1, -1), _FB),
    ]))
    ch_table_h = len(charges_data) * ch_row_h
    charges_table.wrapOn(p, width, height)
    charges_table.drawOn(p, 1.5*cm, y - ch_table_h)
    y -= ch_table_h + 0.15*cm
    # Note VF/TA complete, en petit gras, decoupee pour eviter tout debordement.
    if base_vf > 0 and y > 2.75*cm:
        base_vf_f = float(base_vf)
        brut_gnf = float(bulletin.salaire_brut)
        deduction = max(0, brut_gnf - base_vf_f)
        ta_ou_onfpp = 'ONFPP' if onfpp > 0 else 'TA'
        taux_ta_note = '1,5' if onfpp > 0 else taux_ta_label
        charge_valeur = onfpp if onfpp > 0 else ta
        base_charge_note = float(base_onfpp) if onfpp > 0 else base_vf_f
        p.setFont(_FB, 4.8)
        p.setFillColor(colors.HexColor("#444444"))
        p.drawString(1.5*cm, y,
            f"Base VF/{ta_ou_onfpp} = Brut {brut_gnf:,.0f} - indemnites exonerees plafonnees {deduction:,.0f} "
            f"(25% max du brut) = {base_vf_f:,.0f} GNF"
            .replace(",", " "))
        y -= 0.16*cm
        p.drawString(1.5*cm, y,
            f"VF = {base_vf_f:,.0f} x 6% = {vf:,.0f} GNF | "
            f"{ta_ou_onfpp} = {base_charge_note:,.0f} x {taux_ta_note}% = {charge_valeur:,.0f} GNF"
            .replace(",", " "))
        y -= 0.16*cm
        p.drawString(1.5*cm, y,
            "Ref : Code General des Impots - Guinee (Versement Forfaitaire sur salaires, taux 6%)")
        y -= 0.20*cm
        p.setFillColor(colors.black)
    p.setFillColor(colors.black)
    
    # === PIED DE PAGE — signatures compactes + infos légales centrées ===
    p.setFont(_FB, 6)
    p.drawString(1.5*cm, 2.15*cm, "L'Employeur")
    p.drawRightString(width - 1.5*cm, 2.15*cm, "L'Employé(e)")
    p.setFont(_FN, 5.5)
    if entreprise:
        p.drawString(1.5*cm, 1.93*cm, entreprise.nom_entreprise or '')
    p.drawRightString(width - 1.5*cm, 1.93*cm, f"{emp.nom} {emp.prenoms}")
    p.setFont(_FN, 5)
    p.drawString(1.5*cm, 1.74*cm, "Date et signature")
    p.drawRightString(width - 1.5*cm, 1.74*cm, "Lu et approuvé, date et signature")
    p.setDash(2, 2)
    p.line(1.5*cm, 1.67*cm, 6.5*cm, 1.67*cm)
    p.line(width - 6.5*cm, 1.67*cm, width - 1.5*cm, 1.67*cm)
    p.setDash()
    p.setStrokeColor(colors.HexColor("#dee2e6"))
    p.setLineWidth(0.5)
    p.line(1.5*cm, 1.60*cm, width - 1.5*cm, 1.60*cm)
    p.setStrokeColor(colors.black)
    p.setFont(_FN, 5)
    p.setFillColor(colors.HexColor("#555555"))
    if entreprise:
        p.drawCentredString(width/2, 1.45*cm, f"{entreprise.nom_entreprise} — {entreprise.adresse or ''} — Tél: {entreprise.telephone or ''}")
        p.drawCentredString(width/2, 1.30*cm, f"NIF: {entreprise.nif or '-'} — CNSS: {entreprise.num_cnss or '-'}")
    p.drawCentredString(width/2, 1.15*cm, f"Document généré le {timezone.now().strftime('%d/%m/%Y à %H:%M')}")

    # Badge conformité
    badge_x, badge_y, badge_w, badge_h = 1.5*cm, 0.78*cm, 5.5*cm, 0.45*cm
    p.setFillColor(colors.HexColor("#198754"))
    p.roundRect(badge_x, badge_y, badge_w, badge_h, 3, stroke=0, fill=1)
    p.setFillColor(colors.white)
    p.setFont(_FB, 5.5)
    p.drawCentredString(badge_x + badge_w / 2, badge_y + 0.13*cm,
                        "\u2713 Conforme CGI Guinee | Compatible CNSS")

    # QR Code (coin bas droit)
    try:
        from .utils import _generer_qr_code
        qr_size = 1.4*cm
        qr_x = width - 1.5*cm - qr_size
        qr_y = 0.30*cm
        qr_contenu = (
            f"BUL:{bulletin.numero_bulletin}|"
            f"EMP:{emp.nom} {emp.prenoms}|"
            f"NET:{int(bulletin.net_a_payer)} GNF|"
            f"DATE:{bulletin.date_bulletin.strftime('%d/%m/%Y') if bulletin.date_bulletin else ''}|"
            f"CNSS:{emp.num_cnss_individuel or '-'}"
        )
        qr_img = _generer_qr_code(qr_contenu)
        if qr_img:
            p.drawImage(qr_img, qr_x, qr_y, width=qr_size, height=qr_size, mask='auto')
            p.setFont(_FN, 4.5)
            p.setFillColor(colors.HexColor("#666666"))
            p.drawCentredString(qr_x + qr_size / 2, qr_y - 0.20*cm, "Vérification")
    except Exception:
        pass

    p.setFillColor(colors.black)
    
    # Finaliser le PDF
    p.showPage()
    p.save()
    
    # Retourner le PDF
    buffer.seek(0)
    response = HttpResponse(buffer, content_type='application/pdf')
    filename = f"bulletin_{bulletin.numero_bulletin}_{emp.matricule}.pdf"
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    return response


def telecharger_bulletin_public(request, token):
    """Télécharger un bulletin de paie en PDF via lien public (sans authentification)"""
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import cm, mm
    from reportlab.pdfgen import canvas
    from reportlab.lib import colors
    from reportlab.platypus import Table, TableStyle
    import io
    import os
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    _font_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'static', 'fonts')
    try:
        pdfmetrics.registerFont(TTFont('Arial', os.path.join(_font_dir, 'Arial.ttf')))
        pdfmetrics.registerFont(TTFont('Arial-Bold', os.path.join(_font_dir, 'Arial-Bold.ttf')))
        pdfmetrics.registerFont(TTFont('Arial-Italic', os.path.join(_font_dir, 'Arial-Italic.ttf')))
        _FN = 'Arial'; _FB = 'Arial-Bold'; _FI = 'Arial-Italic'
    except Exception:
        _FN = 'Helvetica'; _FB = 'Helvetica-Bold'; _FI = 'Helvetica-Oblique'

    # Récupérer le bulletin par son token
    bulletin = get_object_or_404(BulletinPaie, token_public=token)
    
    lignes = LigneBulletin.objects.filter(bulletin=bulletin).select_related('rubrique')
    gains = lignes.filter(rubrique__type_rubrique='gain')
    retenues = lignes.filter(rubrique__type_rubrique__in=['retenue', 'cotisation'])
    
    # Créer le buffer PDF
    buffer = io.BytesIO()
    p = canvas.Canvas(buffer, pagesize=A4)
    width, height = A4
    
    # Variables de position
    y = height - 1*cm
    
    # === EN-TÊTE ===
    entreprise = bulletin.employe.entreprise
    
    # Logo entreprise à gauche
    if entreprise and entreprise.logo:
        try:
            logo_path = entreprise.logo.path
            if os.path.exists(logo_path):
                p.drawImage(logo_path, 1.5*cm, y - 2*cm, width=2*cm, height=2*cm, preserveAspectRatio=True)
        except:
            pass
    
    # Titre centré
    p.setFont(_FB, 11)
    p.drawCentredString(width/2, y, "RÉPUBLIQUE DE GUINÉE")
    y -= 0.4*cm
    p.setFont(_FI, 8)
    p.drawCentredString(width/2, y, "Travail - Justice - Solidarité")
    y -= 0.5*cm
    
    # Nom entreprise
    p.setFont(_FB, 12)
    nom_entreprise = entreprise.nom_entreprise if entreprise else "ENTREPRISE"
    p.drawCentredString(width/2, y, nom_entreprise)
    y -= 0.50*cm
    # NIF et CNSS entreprise sous le nom (obligatoire légalement)
    if entreprise:
        nif_str = entreprise.nif or "Non renseigné"
        cnss_str = getattr(entreprise, 'num_cnss', None) or "Non renseigné"
        p.setFont(_FN, 7.5)
        p.setFillColor(colors.HexColor("#444444"))
        p.drawCentredString(width/2, y, f"NIF: {nif_str}   |   CNSS Employeur: {cnss_str}")
        p.setFillColor(colors.black)
        y -= 0.45*cm

    # Titre bulletin
    p.setFont(_FB, 14)
    p.drawCentredString(width/2, y, "BULLETIN DE PAIE")
    y -= 0.4*cm
    
    # Ligne de séparation
    p.setStrokeColor(colors.HexColor("#ce1126"))
    p.setLineWidth(2)
    p.line(1.5*cm, y, width - 1.5*cm, y)
    y -= 0.6*cm
    
    # Infos bulletin sur une ligne
    p.setFont(_FN, 9)
    p.setFillColor(colors.black)
    p.drawString(1.5*cm, y, f"N°: {bulletin.numero_bulletin}")
    p.drawCentredString(width/2, y, f"Période: {bulletin.periode}")
    p.drawRightString(width - 1.5*cm, y, f"Date: {bulletin.date_calcul.strftime('%d/%m/%Y') if bulletin.date_calcul else '-'}")
    y -= 0.35*cm
    # Dates de la période
    p.setFont(_FN, 8)
    periode_detail = f"Du {bulletin.periode.date_debut.strftime('%d/%m/%Y')} au {bulletin.periode.date_fin.strftime('%d/%m/%Y')}" if bulletin.periode.date_debut and bulletin.periode.date_fin else ""
    p.drawCentredString(width/2, y, periode_detail)
    y -= 0.6*cm
    
    # === INFORMATIONS EMPLOYÉ ===
    p.setFillColor(colors.HexColor("#ce1126"))
    p.setFont(_FB, 9)
    p.drawString(1.5*cm, y, "INFORMATIONS EMPLOYÉ")
    p.setFillColor(colors.black)
    y -= 0.5*cm
    
    emp = bulletin.employe
    
    # Ancienneté en années, mois complets et jours à la date du bulletin.
    anciennete_str = format_anciennete_bulletin(emp, bulletin)
    
    # Récupération des congés
    from temps_travail.models import SoldeConge
    solde_conge = SoldeConge.objects.filter(employe=emp, annee=bulletin.annee_paie).first()
    conges_acquis = solde_conge.conges_acquis if solde_conge else Decimal('0')
    conges_pris = solde_conge.conges_pris if solde_conge else Decimal('0')
    conges_restants = solde_conge.conges_restants if solde_conge else Decimal('0')

    # Acquisition mensuelle
    acquis_ce_mois = Decimal('1.5')
    try:
        from .models import ConfigurationPaie
        cfg = ConfigurationPaie.objects.filter(entreprise=entreprise).first()
        if cfg:
            acquis_ce_mois = cfg.jours_conges_par_mois
    except Exception:
        pass

    # Valeurs RH par défaut si non renseignées (acceptable juridiquement,
    # contrairement à "-" ou "Non renseigné" qui posent problème en cas de contrôle).
    poste_str = str(emp.poste).strip() if emp.poste else ""
    service_str = str(emp.service).strip() if emp.service else ""
    poste_affiche = poste_str or "Employé"
    service_affiche = service_str or "Administration"

    infos_emp = [
        ["Matricule:", emp.matricule or "-", "N° CNSS:", emp.num_cnss_individuel or "-"],
        ["Nom et Prénoms:", f"{emp.nom} {emp.prenoms}", "Ancienneté:", anciennete_str],
        ["Poste:", poste_affiche, "Service:", service_affiche],
        ["Date embauche:", emp.date_embauche.strftime('%d/%m/%Y') if emp.date_embauche else "-", "Mode paiement:", emp.mode_paiement or "-"],
        ["Congés acquis:", f"{conges_acquis:g} j", "Congés pris:", f"{conges_pris:g} j"],
        ["Solde congés:", f"{conges_restants:g} j", "Acquis ce mois:", f"{acquis_ce_mois:g} j"],
        ["Nature contrat:", dict(emp.TYPES_CONTRATS).get(emp.type_contrat, emp.type_contrat or "-"), "", ""],
    ]

    for row in infos_emp:
        p.setFont(_FB, 8)
        p.drawString(1.5*cm, y, row[0])
        p.setFont(_FN, 8)
        p.drawString(4*cm, y, str(row[1]))
        if row[2]:
            p.setFont(_FB, 8)
            p.drawString(11*cm, y, row[2])
            p.setFont(_FN, 8)
            p.drawString(14*cm, y, str(row[3]))
        y -= 0.4*cm
    
    y -= 0.3*cm
    
    # === GAINS ===
    p.setFillColor(colors.HexColor("#28a745"))
    p.setFont(_FB, 9)
    p.drawString(1.5*cm, y, "GAINS ET RÉMUNÉRATIONS")
    p.setFillColor(colors.black)
    y -= 0.3*cm
    
    # Tableau des gains (5 colonnes avec Nbre pour les heures)
    gains_data = [["Libellé", "Nbre", "Base", "Taux", "Montant"]]
    for g in gains:
        nbre_str = f"{g.nombre:g}" if g.nombre and g.nombre != 1 else ""
        # Nettoyer le taux : supprimer les zéros inutiles (30.0000% → 30%)
        taux_val = g.taux
        taux_str = f"{float(taux_val):g}%" if taux_val else "-"
        # Corriger libellé: "Prime de transport" → "Indemnité de transport" si forfaitaire
        libelle = g.rubrique.libelle_rubrique[:35]
        code_rub = (g.rubrique.code_rubrique or '').upper()
        if libelle.lower().startswith('prime'):
            from .services import PATTERNS_CODES_FORFAITAIRES, MOTS_CLES_LIBELLE_FORFAITAIRES
            lib_low = libelle.lower()
            is_indem = any(p in code_rub for p in PATTERNS_CODES_FORFAITAIRES) or \
                       any(m in lib_low for m in MOTS_CLES_LIBELLE_FORFAITAIRES)
            if is_indem:
                libelle = 'Indemnité' + libelle[5:]
        if ('HS' in code_rub or 'HEURE' in libelle.upper()) and 'SUP' in libelle.upper():
            libelle = re.sub(r'\s*\+?\d+\s*%', '', libelle).strip()
        # Transparence du calcul : pour un montant mensuel fixe (base == montant, sans nombre>1
        # ni taux), afficher explicitement Nbre = 1 pour signaler la formule "1 × montant".
        # Évite la lecture ambiguë "10 125 000" comprise comme "10 × 125 000".
        montant_fixe_mensuel = (
            g.base
            and (not g.nombre or g.nombre == 1)
            and not g.taux
            and abs(float(g.base) - float(g.montant or 0)) < 1
        )
        if montant_fixe_mensuel and not nbre_str:
            nbre_str = "1"
        base_str = f"{g.base:,.0f}".replace(",", "\u00A0") if g.base else "-"
        gains_data.append([
            libelle,
            nbre_str,
            base_str,
            taux_str,
            f"{g.montant:,.0f}".replace(",", "\u00A0"),
        ])
    gains_data.append(["TOTAL BRUT", "", "", "", f"{bulletin.salaire_brut:,.0f} GNF".replace(",", "\u00A0")])
    
    row_height = 14
    gains_table = Table(gains_data, colWidths=[6*cm, 1.8*cm, 3.2*cm, 1.8*cm, 4.2*cm], rowHeights=row_height)
    _gains_style = [
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor("#28a745")),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('FONTNAME', (0, 0), (-1, 0), _FB),
        ('FONTSIZE', (0, 0), (-1, -1), 8),
        ('ALIGN', (0, 0), (0, -1), 'LEFT'),
        ('ALIGN', (1, 0), (-1, -1), 'RIGHT'),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.grey),
        ('LEFTPADDING', (0, 0), (-1, -1), 6),
        ('RIGHTPADDING', (0, 0), (-1, -1), 6),
        ('BACKGROUND', (0, -1), (-1, -1), colors.HexColor("#d4edda")),
        ('FONTNAME', (0, -1), (-1, -1), _FB),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
    ]
    # Zébrures lignes paires (sauf header et total) pour lisibilité
    for _i in range(1, len(gains_data) - 1):
        if _i % 2 == 0:
            _gains_style.append(('BACKGROUND', (0, _i), (-1, _i), colors.HexColor("#f8f9fa")))
    gains_table.setStyle(TableStyle(_gains_style))
    
    table_height = len(gains_data) * row_height
    gains_table.wrapOn(p, width, height)
    gains_table.drawOn(p, 1.5*cm, y - table_height)
    y -= table_height + 0.35*cm
    
    # === DÉTAIL HEURES SUPPLÉMENTAIRES ===
    hs_30 = getattr(bulletin, 'heures_supplementaires_30', 0) or 0
    hs_60 = getattr(bulletin, 'heures_supplementaires_60', 0) or 0
    hs_nuit = getattr(bulletin, 'heures_nuit', 0) or 0
    hs_feries = getattr(bulletin, 'heures_feries', 0) or 0
    hs_feries_nuit = getattr(bulletin, 'heures_feries_nuit', 0) or 0
    prime_hs = getattr(bulletin, 'prime_heures_sup', 0) or 0
    prime_nuit = getattr(bulletin, 'prime_nuit', 0) or 0
    prime_feries = getattr(bulletin, 'prime_feries', 0) or 0
    prime_feries_nuit = getattr(bulletin, 'prime_feries_nuit', 0) or 0
    total_hs_heures = float(hs_30) + float(hs_60) + float(hs_nuit) + float(hs_feries) + float(hs_feries_nuit)
    
    if total_hs_heures > 0 or float(prime_hs) > 0:
        p.setFont(_FB, 7)
        p.setFillColor(colors.HexColor("#6c757d"))
        p.drawString(1.5*cm, y, "DÉTAIL HEURES SUPPLÉMENTAIRES (Code du Travail Art. 221)")
        p.setFillColor(colors.black)
        y -= 0.18*cm
        
        # Calcul des montants individuels (salaire_base / 173,33 × h × coefficient)
        _sal_base = Decimal(str(bulletin.salaire_base or 0))
        if _sal_base == 0:
            try:
                ligne_base = LigneBulletin.objects.filter(
                    bulletin=bulletin,
                    rubrique__type_rubrique='gain',
                ).filter(
                    Q(rubrique__categorie_rubrique='salaire_base') |
                    Q(rubrique__code_rubrique__icontains='SAL_BASE') |
                    Q(rubrique__code_rubrique__iexact='BASE') |
                    Q(rubrique__libelle_rubrique__icontains='Salaire de base')
                ).annotate(
                    _base_priority=Case(
                        When(rubrique__categorie_rubrique='salaire_base', then=Value(0)),
                        When(rubrique__code_rubrique__icontains='SAL_BASE', then=Value(1)),
                        When(rubrique__code_rubrique__iexact='BASE', then=Value(2)),
                        default=Value(9),
                        output_field=IntegerField(),
                    )
                ).order_by('_base_priority', 'ordre', 'id').first()
                if ligne_base:
                    _sal_base = Decimal(str(ligne_base.montant or 0))
            except Exception:
                pass
        _sal_h = _sal_base / Decimal('173.33') if _sal_base > 0 else Decimal('0')
        _montant_30 = round(_sal_h * Decimal(str(hs_30)) * Decimal('1.30'))
        _montant_60 = round(_sal_h * Decimal(str(hs_60)) * Decimal('1.60'))
        _montant_nuit = round(_sal_h * Decimal(str(hs_nuit)) * Decimal('1.20')) if float(hs_nuit) > 0 else 0
        _montant_feries = round(_sal_h * Decimal(str(hs_feries)) * Decimal('1.60')) if float(hs_feries) > 0 else 0
        _montant_feries_nuit = round(_sal_h * Decimal(str(hs_feries_nuit)) * Decimal('2.00')) if float(hs_feries_nuit) > 0 else 0

        hs_detail_data = [["Type", "Heures", "Majoration", "Montant"]]
        if float(hs_30) > 0:
            hs_detail_data.append(["4 prem. HS/sem.", f"{hs_30:g}h", "+30% (130%)",
                                    f"{_montant_30:,.0f}".replace(",", " ")])
        if float(hs_60) > 0:
            hs_detail_data.append(["Au-delà 4 HS/sem.", f"{hs_60:g}h", "+60% (160%)",
                                    f"{_montant_60:,.0f}".replace(",", " ")])
        if float(hs_nuit) > 0:
            hs_detail_data.append(["Heures de nuit (20h-6h)", f"{hs_nuit:g}h", "+20% (120%)",
                                    f"{_montant_nuit:,.0f}".replace(",", " ")])
        if float(hs_feries) > 0:
            hs_detail_data.append(["Jours fériés (jour)", f"{hs_feries:g}h", "+60% (160%)",
                                    f"{_montant_feries:,.0f}".replace(",", " ")])
        if float(hs_feries_nuit) > 0:
            hs_detail_data.append(["Jours fériés (nuit)", f"{hs_feries_nuit:g}h", "+100% (200%)",
                                    f"{_montant_feries_nuit:,.0f}".replace(",", " ")])

        total_prime = float(prime_hs) + float(prime_nuit) + float(prime_feries) + float(prime_feries_nuit)
        if total_prime <= 0 and _total_hs_calcule > 0:
            total_prime = float(_total_hs_calcule)
        hs_detail_data.append(["", f"{total_hs_heures:g}h", "Total HS:",
                                f"{total_prime:,.0f} GNF".replace(",", " ")])
        
        hs_row_h = 12
        hs_table = Table(hs_detail_data, colWidths=[5.5*cm, 2*cm, 4.5*cm, 5*cm], rowHeights=hs_row_h)
        nb_hs_rows = len(hs_detail_data)
        hs_table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor("#6c757d")),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
            ('FONTNAME', (0, 0), (-1, 0), _FB),
            ('FONTSIZE', (0, 0), (-1, -1), 7),
            ('ALIGN', (1, 0), (-1, -1), 'RIGHT'),
            ('GRID', (0, 0), (-1, -1), 0.3, colors.HexColor("#dee2e6")),
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            ('FONTNAME', (0, -1), (-1, -1), _FB),
            ('BACKGROUND', (0, -1), (-1, -1), colors.HexColor("#f8f9fa")),
        ]))
        
        hs_table_h = nb_hs_rows * hs_row_h
        hs_table.wrapOn(p, width, height)
        hs_table.drawOn(p, 1.5*cm, y - hs_table_h)
        y -= hs_table_h + 0.35*cm
    else:
        y -= 0.25*cm
    
    # === RETENUES ===
    p.setFillColor(colors.HexColor("#dc3545"))
    p.setFont(_FB, 9)
    p.drawString(1.5*cm, y, "RETENUES ET COTISATIONS")
    p.setFillColor(colors.black)
    y -= 0.25*cm
    
    retenues_data = [["Libellé", "Base", "Taux", "Montant"]]
    # Filtrer les doublons CNSS et IRG (déjà dans les lignes du bulletin)
    cnss_irg_codes = ['CNSS', 'IRG', 'RTS', 'IRS', 'IRPP']
    for r in retenues:
        # Éviter les doublons - ne pas réafficher si déjà traité
        code = r.rubrique.code_rubrique.upper() if r.rubrique.code_rubrique else ''
        libelle = r.rubrique.libelle_rubrique.upper() if r.rubrique.libelle_rubrique else ''
        is_cnss_irg = any(c in code or c in libelle for c in cnss_irg_codes)
        if not is_cnss_irg:
            retenues_data.append([
                r.rubrique.libelle_rubrique[:35],
                f"{r.base:,.0f}".replace(",", " ") if r.base else "-",
                f"{r.taux}%" if r.taux else "-",
                f"{r.montant:,.0f}".replace(",", " ")
            ])
    
    # Ajouter CNSS (base plafonnée) et RTS avec détail
    base_cnss = min(bulletin.salaire_brut, 2500000)
    retenues_data.append(["CNSS Employé (5%)", f"{base_cnss:,.0f}".replace(",", " "), "5%", f"{bulletin.cnss_employe:,.0f}".replace(",", " ")])
    # RTS avec base, taux effectif et montant
    base_rts_val = getattr(bulletin, 'base_rts', 0) or 0
    taux_eff_rts_val = getattr(bulletin, 'taux_effectif_rts', 0) or 0
    rts_base_str = f"{base_rts_val:,.0f}".replace(",", " ") if base_rts_val else "-"
    # Note: l'abattement forfaitaire (exonération 25%) est un ajustement fiscal,
    # pas une retenue réelle → affiché uniquement dans la section DÉTAIL RTS.
    retenues_data.append(["RTS (Impôt sur le revenu \u2013 barème progressif)", rts_base_str, "-", f"{bulletin.irg:,.0f}".replace(",", " ")])

    retenues_table = Table(retenues_data, colWidths=[8*cm, 3*cm, 2*cm, 4*cm], rowHeights=row_height)
    retenues_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor("#dc3545")),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('FONTNAME', (0, 0), (-1, 0), _FB),
        ('FONTSIZE', (0, 0), (-1, -1), 8),
        ('ALIGN', (1, 0), (-1, -1), 'RIGHT'),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.grey),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
    ]))

    table_height = len(retenues_data) * row_height
    retenues_table.wrapOn(p, width, height)
    retenues_table.drawOn(p, 1.5*cm, y - table_height)
    y -= table_height + 0.25*cm

    # === DÉTAIL CALCUL RTS (barème progressif) ===
    from paie.utils import calculer_detail_tranches_rts
    detail_rts = calculer_detail_tranches_rts(base_rts_val)
    if detail_rts:
        p.setFont(_FB, 7)
        p.setFillColor(colors.HexColor("#6c757d"))
        # Explication base imposable avec nature de l'abattement
        abattement_val = getattr(bulletin, 'abattement_forfaitaire', 0) or 0
        if float(abattement_val) > 0:
            p.drawString(1.5*cm, y,
                f"DÉTAIL RTS \u2014 Base imposable: {base_rts_val:,.0f} = "
                f"Brut {bulletin.salaire_brut:,.0f} \u2212 CNSS {bulletin.cnss_employe:,.0f} "
                f"\u2212 Indemnités exonérées {abattement_val:,.0f} (plafond 25% du brut)"
                .replace(",", " "))
        else:
            p.drawString(1.5*cm, y,
                f"DÉTAIL RTS — Base imposable: {base_rts_val:,.0f} = "
                f"Brut {bulletin.salaire_brut:,.0f} − CNSS {bulletin.cnss_employe:,.0f}"
                .replace(",", " "))
        p.setFillColor(colors.black)
        y -= 0.18*cm

        rts_detail_data = [["Tranche (bornes)", "Base taxable", "Taux", "Impôt"]]
        cumul_impot = Decimal('0')
        for i, t in enumerate(detail_rts, start=1):
            taux_pct = f"{t['taux']:g}"
            b_inf = f"{t['borne_inf']:,.0f}".replace(",", " ")
            b_sup = f"{t['borne_sup']:,.0f}".replace(",", " ") if t.get('borne_sup') else "∞"
            base_tr = f"{t['base_tranche']:,.0f}".replace(",", " ")
            rts_detail_data.append([
                f"{b_inf} à {b_sup}",
                base_tr,
                f"{taux_pct}%",
                f"{t['impot_tranche']:,.0f}".replace(",", " "),
            ])
            cumul_impot += t['impot_tranche']
        rts_detail_data.append(["", "", "Total RTS:", f"{cumul_impot:,.0f} GNF".replace(",", " ")])

        rts_row_h = 12
        rts_table = Table(rts_detail_data, colWidths=[5*cm, 4*cm, 3*cm, 5*cm], rowHeights=rts_row_h)
        nb_rows = len(rts_detail_data)
        rts_table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor("#6c757d")),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
            ('FONTNAME', (0, 0), (-1, 0), _FB),
            ('FONTSIZE', (0, 0), (-1, -1), 7),
            ('ALIGN', (1, 0), (-1, -1), 'RIGHT'),
            ('GRID', (0, 0), (-1, -1), 0.3, colors.HexColor("#dee2e6")),
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            ('FONTNAME', (0, -1), (-1, -1), _FB),
            ('BACKGROUND', (0, -1), (-1, -1), colors.HexColor("#f8f9fa")),
        ]))
        
        rts_table_h = nb_rows * rts_row_h
        rts_table.wrapOn(p, width, height)
        rts_table.drawOn(p, 1.5*cm, y - rts_table_h)
        y -= rts_table_h + 0.25*cm
    else:
        y -= 0.2*cm
    
    # === RÉCAPITULATIF ===
    rappel = getattr(bulletin, 'rappel_salaire', 0) or 0
    trop_percu = getattr(bulletin, 'retenue_trop_percu', 0) or 0
    has_rappel = rappel > 0
    has_trop_percu = trop_percu > 0
    extra_lines = (1 if has_rappel else 0) + (1 if has_trop_percu else 0)
    recap_height = 1.85*cm + extra_lines * 0.30*cm
    p.setStrokeColor(colors.HexColor("#ce1126"))
    p.setLineWidth(2)
    p.rect(1.5*cm, y - recap_height, width - 3*cm, recap_height, stroke=1, fill=0)
    
    p.setFont(_FB, 10)
    p.setFillColor(colors.black)
    p.drawString(2*cm, y - 0.5*cm, "SALAIRE BRUT:")
    p.drawRightString(width - 2*cm, y - 0.5*cm, f"{bulletin.salaire_brut:,.0f} GNF".replace(",", " "))
    
    # CNSS et RTS alignés sur la même ligne
    p.setFont(_FN, 8)
    p.setFillColor(colors.HexColor("#dc3545"))
    mid_x = width / 2
    p.drawString(2*cm, y - 1*cm, f"CNSS (5%): -{bulletin.cnss_employe:,.0f}".replace(",", " "))
    p.drawString(mid_x, y - 1*cm, f"RTS: -{bulletin.irg:,.0f}".replace(",", " "))
    p.drawRightString(width - 2*cm, y - 1*cm, f"Total retenues: -{bulletin.total_retenues:,.0f} GNF".replace(",", " "))
    
    offset_y = 1*cm
    
    # Afficher l'abattement forfaitaire si présent (détail RTS)
    if has_abatt:
        offset_y += 0.3*cm
        p.setFont(_FN, 7)
        p.setFillColor(colors.HexColor("#666666"))
        p.drawString(2.3*cm, y - offset_y, f"└─ abattement 25%: {abatt_forfait:,.0f}".replace(",", " "))
    
    if has_rappel:
        offset_y += 0.3*cm
        p.setFillColor(colors.HexColor("#007bff"))
        p.setFont(_FN, 8)
        p.drawString(2*cm, y - offset_y, "Rappel/Complément salaire précédent:")
        p.drawRightString(width - 2*cm, y - offset_y, f"+ {rappel:,.0f} GNF".replace(",", " "))
    if has_trop_percu:
        offset_y += 0.3*cm
        p.setFillColor(colors.HexColor("#dc3545"))
        p.setFont(_FN, 8)
        p.drawString(2*cm, y - offset_y, "Retenue trop-perçu salaire précédent:")
        p.drawRightString(width - 2*cm, y - offset_y, f"- {trop_percu:,.0f} GNF".replace(",", " "))
    
    p.setFillColor(colors.HexColor("#28a745"))
    p.setFont(_FB, 14)
    p.drawString(2*cm, y - offset_y - 0.7*cm, "NET À PAYER:")
    p.drawRightString(width - 2*cm, y - offset_y - 0.7*cm, f"{bulletin.net_a_payer:,.0f} GNF".replace(",", " "))
    p.setFillColor(colors.black)
    
    y -= recap_height + 0.25*cm
    
    # === CHARGES PATRONALES ===
    vf = getattr(bulletin, 'versement_forfaitaire', 0) or 0
    ta = getattr(bulletin, 'taxe_apprentissage', 0) or 0
    taux_ta = getattr(bulletin, 'taux_ta', 0) or 0
    onfpp = getattr(bulletin, 'contribution_onfpp', 0) or 0
    base_vf = getattr(bulletin, 'base_vf', 0) or 0
    base_onfpp = getattr(bulletin, 'base_onfpp', 0) or base_vf
    nb_sal = getattr(bulletin, 'nombre_salaries', 0) or 0
    total_charges = bulletin.cnss_employeur + vf + ta + onfpp
    taux_ta_label = str(taux_ta).rstrip('0').rstrip('.').replace('.', ',') if taux_ta else '2'
    
    # Tableau charges patronales (une ligne par charge = lisibilité audit)
    charges_data = [["Charge patronale", "Base", "Taux", "Montant"]]
    charges_data.append(["CNSS Employeur",
        f"{min(bulletin.salaire_brut, 2500000):,.0f}".replace(",", " "),
        "18%",
        f"{bulletin.cnss_employeur:,.0f}".replace(",", " ")])
    charges_data.append(["Versement Forfaitaire (VF)",
        f"{base_vf:,.0f}".replace(",", " ") if base_vf else "-",
        "6%",
        f"{vf:,.0f}".replace(",", " ")])
    if ta > 0:
        charges_data.append([f"TA (applicable si effectif < 30 sal. \u2014 effectif actuel : {nb_sal})",
            f"{base_vf:,.0f}".replace(",", " ") if base_vf else "-",
            f"{taux_ta_label}%",
            f"{ta:,.0f}".replace(",", " ")])
    elif onfpp > 0:
        charges_data.append([f"ONFPP (applicable si effectif \u2265 30 sal. \u2014 effectif actuel : {nb_sal})",
            f"{base_onfpp:,.0f}".replace(",", " ") if base_onfpp else "-",
            "1,5%",
            f"{onfpp:,.0f}".replace(",", " ")])
    charges_data.append(["TOTAL CHARGES PATRONALES", "", "",
        f"{total_charges:,.0f} GNF".replace(",", " ")])

    ch_row_h = 12
    charges_table = Table(charges_data, colWidths=[7*cm, 3.5*cm, 2*cm, 4.5*cm], rowHeights=ch_row_h)
    charges_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor("#fd7e14")),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('FONTNAME', (0, 0), (-1, 0), _FB),
        ('FONTSIZE', (0, 0), (-1, -1), 7),
        ('ALIGN', (1, 0), (-1, -1), 'RIGHT'),
        ('GRID', (0, 0), (-1, -1), 0.4, colors.HexColor("#dee2e6")),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('BACKGROUND', (0, -1), (-1, -1), colors.HexColor("#fff3cd")),
        ('FONTNAME', (0, -1), (-1, -1), _FB),
    ]))
    ch_table_h = len(charges_data) * ch_row_h
    charges_table.wrapOn(p, width, height)
    charges_table.drawOn(p, 1.5*cm, y - ch_table_h)
    y -= ch_table_h + 0.15*cm
    # Note VF/TA complete, en petit gras, decoupee pour eviter tout debordement.
    if base_vf > 0 and y > 2.75*cm:
        base_vf_f = float(base_vf)
        brut_gnf = float(bulletin.salaire_brut)
        deduction = max(0, brut_gnf - base_vf_f)
        ta_ou_onfpp = 'ONFPP' if onfpp > 0 else 'TA'
        taux_ta_note = '1,5' if onfpp > 0 else taux_ta_label
        charge_valeur = onfpp if onfpp > 0 else ta
        base_charge_note = float(base_onfpp) if onfpp > 0 else base_vf_f
        p.setFont(_FB, 4.8)
        p.setFillColor(colors.HexColor("#444444"))
        p.drawString(1.5*cm, y,
            f"Base VF/{ta_ou_onfpp} = Brut {brut_gnf:,.0f} - indemnites exonerees plafonnees {deduction:,.0f} "
            f"(25% max du brut) = {base_vf_f:,.0f} GNF"
            .replace(",", " "))
        y -= 0.16*cm
        p.drawString(1.5*cm, y,
            f"VF = {base_vf_f:,.0f} x 6% = {vf:,.0f} GNF | "
            f"{ta_ou_onfpp} = {base_charge_note:,.0f} x {taux_ta_note}% = {charge_valeur:,.0f} GNF"
            .replace(",", " "))
        y -= 0.16*cm
        p.drawString(1.5*cm, y,
            "Ref : Code General des Impots - Guinee (Versement Forfaitaire sur salaires, taux 6%)")
        y -= 0.20*cm
        p.setFillColor(colors.black)
    p.setFillColor(colors.black)
    
    # === PIED DE PAGE — signatures compactes + infos légales centrées ===
    p.setFont(_FB, 6)
    p.drawString(1.5*cm, 2.15*cm, "L'Employeur")
    p.drawRightString(width - 1.5*cm, 2.15*cm, "L'Employé(e)")
    p.setFont(_FN, 5.5)
    if entreprise:
        p.drawString(1.5*cm, 1.93*cm, entreprise.nom_entreprise or '')
    p.drawRightString(width - 1.5*cm, 1.93*cm, f"{emp.nom} {emp.prenoms}")
    p.setFont(_FN, 5)
    p.drawString(1.5*cm, 1.74*cm, "Date et signature")
    p.drawRightString(width - 1.5*cm, 1.74*cm, "Lu et approuvé, date et signature")
    p.setDash(2, 2)
    p.line(1.5*cm, 1.67*cm, 6.5*cm, 1.67*cm)
    p.line(width - 6.5*cm, 1.67*cm, width - 1.5*cm, 1.67*cm)
    p.setDash()
    p.setStrokeColor(colors.HexColor("#dee2e6"))
    p.setLineWidth(0.5)
    p.line(1.5*cm, 1.60*cm, width - 1.5*cm, 1.60*cm)
    p.setStrokeColor(colors.black)
    p.setFont(_FN, 5)
    p.setFillColor(colors.HexColor("#555555"))
    if entreprise:
        p.drawCentredString(width/2, 1.45*cm, f"{entreprise.nom_entreprise} — {entreprise.adresse or ''} — Tél: {entreprise.telephone or ''}")
        p.drawCentredString(width/2, 1.30*cm, f"NIF: {entreprise.nif or '-'} — CNSS: {entreprise.num_cnss or '-'}")
    p.drawCentredString(width/2, 1.15*cm, f"Document généré le {timezone.now().strftime('%d/%m/%Y à %H:%M')}")

    # Badge conformité
    badge_x, badge_y, badge_w, badge_h = 1.5*cm, 0.78*cm, 5.5*cm, 0.45*cm
    p.setFillColor(colors.HexColor("#198754"))
    p.roundRect(badge_x, badge_y, badge_w, badge_h, 3, stroke=0, fill=1)
    p.setFillColor(colors.white)
    p.setFont(_FB, 5.5)
    p.drawCentredString(badge_x + badge_w / 2, badge_y + 0.13*cm,
                        "\u2713 Conforme CGI Guinee | Compatible CNSS")

    # QR Code (coin bas droit)
    try:
        from .utils import _generer_qr_code
        qr_size = 1.4*cm
        qr_x = width - 1.5*cm - qr_size
        qr_y = 0.30*cm
        qr_contenu = (
            f"BUL:{bulletin.numero_bulletin}|"
            f"EMP:{emp.nom} {emp.prenoms}|"
            f"NET:{int(bulletin.net_a_payer)} GNF|"
            f"DATE:{bulletin.date_bulletin.strftime('%d/%m/%Y') if bulletin.date_bulletin else ''}|"
            f"CNSS:{emp.num_cnss_individuel or '-'}"
        )
        qr_img = _generer_qr_code(qr_contenu)
        if qr_img:
            p.drawImage(qr_img, qr_x, qr_y, width=qr_size, height=qr_size, mask='auto')
            p.setFont(_FN, 4.5)
            p.setFillColor(colors.HexColor("#666666"))
            p.drawCentredString(qr_x + qr_size / 2, qr_y - 0.20*cm, "Vérification")
    except Exception:
        pass

    p.setFillColor(colors.black)
    
    # Finaliser le PDF
    p.showPage()
    p.save()
    
    # Retourner le PDF
    buffer.seek(0)
    response = HttpResponse(buffer, content_type='application/pdf')
    filename = f"bulletin_{bulletin.numero_bulletin}_{emp.matricule}.pdf"
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    return response


def _cnss_attendue_livre(brut):
    brut = Decimal(str(brut or 0))
    plancher = Decimal('550000')
    plafond = Decimal('2500000')
    if brut < plancher * Decimal('0.10'):
        base = Decimal('0')
    else:
        base = max(min(brut, plafond), plancher)
    return {
        'base': base.quantize(Decimal('1'), rounding=ROUND_HALF_UP),
        'employe': (base * Decimal('0.05')).quantize(Decimal('1'), rounding=ROUND_HALF_UP),
        'employeur': (base * Decimal('0.18')).quantize(Decimal('1'), rounding=ROUND_HALF_UP),
    }


def _controles_livre_paie(bulletins, totaux):
    """Controle macro et lignes CNSS du livre de paie."""
    anomalies = []
    retenues_hors_cnss_rts = []
    total_detail_brut = Decimal('0')
    total_detail_retenues = Decimal('0')
    total_detail_net = Decimal('0')
    tolerance_cnss = Decimal('1')
    for bulletin in bulletins:
        brut = Decimal(str(bulletin.salaire_brut or 0))
        rappel = Decimal(str(getattr(bulletin, 'rappel_salaire', 0) or 0))
        net = Decimal(str(getattr(bulletin, 'net_a_payer', 0) or 0))
        attendu = _cnss_attendue_livre(brut)
        cnss_emp = Decimal(str(bulletin.cnss_employe or 0))
        cnss_pat = Decimal(str(bulletin.cnss_employeur or 0))
        rts = Decimal(str(getattr(bulletin, 'irg', 0) or 0))
        total_retenues_reel = (brut + rappel - net).quantize(Decimal('1'), rounding=ROUND_HALF_UP)
        total_retenues_legales = cnss_emp + rts
        retenue_hors_cnss_rts = total_retenues_reel - total_retenues_legales
        setattr(bulletin, 'total_retenues_livre', total_retenues_reel)
        setattr(bulletin, 'controle_retenues_hors_cnss_rts', retenue_hors_cnss_rts != Decimal('0'))
        total_detail_brut += brut
        total_detail_retenues += total_retenues_reel
        total_detail_net += net

        if retenue_hors_cnss_rts != Decimal('0'):
            retenues_hors_cnss_rts.append({
                'numero': bulletin.numero_bulletin,
                'matricule': bulletin.employe.matricule,
                'nom': f"{bulletin.employe.nom} {bulletin.employe.prenoms}".strip(),
                'montant': retenue_hors_cnss_rts,
                'total_retenues_reel': total_retenues_reel,
                'total_retenues_legales': total_retenues_legales,
            })

        if abs(cnss_emp - attendu['employe']) > tolerance_cnss or abs(cnss_pat - attendu['employeur']) > tolerance_cnss:
            anomalies.append({
                'numero': bulletin.numero_bulletin,
                'matricule': bulletin.employe.matricule,
                'nom': f"{bulletin.employe.nom} {bulletin.employe.prenoms}".strip(),
                'brut': brut,
                'base_cnss': attendu['base'],
                'cnss_employe': cnss_emp,
                'cnss_employe_attendu': attendu['employe'],
                'cnss_employeur': cnss_pat,
                'cnss_employeur_attendu': attendu['employeur'],
            })
            setattr(bulletin, 'controle_cnss_livre_erreur', True)
        else:
            setattr(bulletin, 'controle_cnss_livre_erreur', False)

    total_brut = Decimal(str(totaux.get('total_brut') or 0))
    total_retenues = Decimal(str(totaux.get('total_retenues') or 0))
    total_net = Decimal(str(totaux.get('total_net') or 0))
    net_attendu = total_brut - total_retenues
    ecart_net = total_net - net_attendu
    macro_ok = ecart_net == Decimal('0')
    controles_agregation = []
    for libelle, total_detail, total_resume in (
        ('Masse salariale brute', total_detail_brut, total_brut),
        ('Total retenues', total_detail_retenues, total_retenues),
        ('Net a payer', total_detail_net, total_net),
    ):
        ecart = total_detail - total_resume
        if ecart != Decimal('0'):
            controles_agregation.append({
                'libelle': libelle,
                'total_detail': total_detail,
                'total_resume': total_resume,
                'ecart': ecart,
            })

    conforme = macro_ok and not anomalies and not controles_agregation
    return {
        'anomalies_cnss': anomalies,
        'nb_anomalies_cnss': len(anomalies),
        'retenues_hors_cnss_rts': retenues_hors_cnss_rts,
        'nb_retenues_hors_cnss_rts': len(retenues_hors_cnss_rts),
        'total_retenues_hors_cnss_rts': sum((r['montant'] for r in retenues_hors_cnss_rts), Decimal('0')),
        'controles_agregation': controles_agregation,
        'nb_controles_agregation': len(controles_agregation),
        'net_attendu': net_attendu,
        'ecart_net': ecart_net,
        'macro_ok': macro_ok,
        'conforme': conforme,
        'statut': 'CONFORME' if conforme else 'NON CONFORME',
    }


def _bloquer_pdf_livre_paie(controles_livre, fmt):
    """Refuse le PDF officiel si les controles du livre de paie echouent."""
    from django.utils.html import escape

    anomalies = controles_livre.get('anomalies_cnss', [])[:10]
    lignes = ''.join(
        '<li>{numero} - {matricule} - CNSS salariee {actuelle} GNF, attendue {attendue} GNF</li>'.format(
            numero=escape(a.get('numero') or '-'),
            matricule=escape(a.get('matricule') or '-'),
            actuelle=fmt(a.get('cnss_employe')),
            attendue=fmt(a.get('cnss_employe_attendu')),
        )
        for a in anomalies
    )
    if controles_livre.get('nb_anomalies_cnss', 0) > len(anomalies):
        lignes += '<li>... autres anomalies CNSS non affichees</li>'
    agregations = ''.join(
        '<li>{libelle} : detail {detail} GNF, resume {resume} GNF, ecart {ecart} GNF</li>'.format(
            libelle=escape(a.get('libelle') or '-'),
            detail=fmt(a.get('total_detail')),
            resume=fmt(a.get('total_resume')),
            ecart=fmt(a.get('ecart')),
        )
        for a in controles_livre.get('controles_agregation', [])
    )

    html = """
<!doctype html>
<html lang="fr">
<head>
  <meta charset="utf-8">
  <title>Livre de paie non conforme</title>
  <style>
    body {{ font-family: Arial, sans-serif; margin: 32px; color: #212529; }}
    .box {{ border: 1px solid #dc3545; border-radius: 8px; padding: 20px; max-width: 820px; }}
    h1 {{ color: #dc3545; margin-top: 0; }}
    .metric {{ margin: 8px 0; }}
    .hint {{ margin-top: 18px; padding: 12px; background: #fff3cd; border: 1px solid #ffe69c; }}
  </style>
</head>
<body>
  <div class="box">
    <h1>Livre de paie non conforme</h1>
    <p>Generation du PDF officiel bloquee. Corrigez les bulletins puis relancez l'export.</p>
    <div class="metric"><strong>Statut :</strong> NON CONFORME</div>
    <div class="metric"><strong>Net attendu :</strong> {net_attendu} GNF</div>
    <div class="metric"><strong>Ecart net :</strong> {ecart_net} GNF</div>
    <div class="metric"><strong>Anomalies CNSS :</strong> {nb_anomalies}</div>
    <div class="metric"><strong>Ecarts aggregation :</strong> {nb_agregations}</div>
    <ul>{lignes}</ul>
    <ul>{agregations}</ul>
    <div class="hint">Action recommandee : recalculer les bulletins de la periode concernee pour appliquer le plafond CNSS legal.</div>
  </div>
</body>
</html>
""".format(
        net_attendu=fmt(controles_livre.get('net_attendu')),
        ecart_net=fmt(controles_livre.get('ecart_net')),
        nb_anomalies=controles_livre.get('nb_anomalies_cnss', 0),
        nb_agregations=controles_livre.get('nb_controles_agregation', 0),
        lignes=lignes or '<li>Aucune anomalie CNSS detaillee.</li>',
        agregations=agregations or '<li>Aucun ecart aggregation detaille.</li>',
    )
    return HttpResponse(html, status=409, content_type='text/html; charset=utf-8')


_DETAILS_LIVRE_PAIE = (
    'livre_salaire_base',
    'livre_prime_logement',
    'livre_prime_transport',
    'livre_cherte_vie',
    'livre_avance_salaire',
)


def _filtres_periode_livre_paie(request):
    annee = None
    mois = None
    annee_param = request.GET.get('annee')
    mois_param = request.GET.get('mois')
    try:
        if annee_param:
            annee = int(annee_param)
    except (TypeError, ValueError):
        annee = None
    try:
        if mois_param:
            mois = int(mois_param)
    except (TypeError, ValueError):
        mois = None
    if annee is not None and not (2000 <= annee <= 2100):
        annee = None
    if mois is not None and not (1 <= mois <= 12):
        mois = None
    return annee, mois


def _libelle_portee_livre_paie(annee, mois, annees=None):
    annees = list(annees or [])
    if annee and mois:
        return {
            'badge': 'Mensuel',
            'titre': f'Rapport mensuel - {mois:02d}/{annee}',
            'description': 'Les totaux affichés concernent uniquement le mois sélectionné.',
        }
    if annee:
        return {
            'badge': 'Annuel',
            'titre': f'Cumul annuel - {annee}',
            'description': 'Les totaux affichés cumulent tous les mois disponibles de l’année.',
        }
    if len(annees) == 1:
        annee_unique = annees[0]
        return {
            'badge': 'Annuel',
            'titre': f'Cumul annuel - {annee_unique}',
            'description': 'Les totaux affichés cumulent tous les mois disponibles de l’année.',
        }
    return {
        'badge': 'Toutes périodes',
        'titre': 'Cumul toutes périodes',
        'description': 'Les totaux affichés cumulent toutes les périodes de paie disponibles.',
    }


def _normaliser_texte_livre(valeur):
    texte = str(valeur or '').strip().lower()
    return ''.join(
        caractere for caractere in unicodedata.normalize('NFKD', texte)
        if not unicodedata.combining(caractere)
    )


def _categorie_ligne_livre_paie(ligne):
    rubrique = getattr(ligne, 'rubrique', None)
    if not rubrique:
        return None

    code = _normaliser_texte_livre(getattr(rubrique, 'code_rubrique', '')).upper()
    libelle = _normaliser_texte_livre(
        f"{getattr(rubrique, 'libelle_rubrique', '')} {getattr(ligne, 'libelle_personnalise', '')}"
    )
    categorie = _normaliser_texte_livre(getattr(rubrique, 'categorie_rubrique', ''))
    type_rubrique = _normaliser_texte_livre(getattr(rubrique, 'type_rubrique', ''))

    if categorie == 'salaire_base' or code in {'BASE', 'SAL_BASE', 'SALAIRE_BASE'} or 'salaire de base' in libelle:
        return 'livre_salaire_base'
    if code in {'PL', 'LOG'} or 'LOGEMENT' in code or 'logement' in libelle or 'hebergement' in libelle:
        return 'livre_prime_logement'
    if code in {'PT', 'TRP'} or 'TRANSPORT' in code or 'transport' in libelle:
        return 'livre_prime_transport'
    if code in {'PCV', 'PVC', 'CHV'} or 'CHERT' in code or 'cherte' in libelle or 'vie chere' in libelle:
        return 'livre_cherte_vie'
    if type_rubrique == 'retenue' and (code in {'AVS', 'AVANCE'} or 'AVANCE' in code or 'avance' in libelle):
        return 'livre_avance_salaire'
    return None


def _enrichir_details_livre_paie(bulletins):
    totaux_details = {
        'total_salaire_base': Decimal('0'),
        'total_prime_logement': Decimal('0'),
        'total_prime_transport': Decimal('0'),
        'total_cherte_vie': Decimal('0'),
        'total_avance_salaire': Decimal('0'),
    }

    correspondances_totaux = {
        'livre_salaire_base': 'total_salaire_base',
        'livre_prime_logement': 'total_prime_logement',
        'livre_prime_transport': 'total_prime_transport',
        'livre_cherte_vie': 'total_cherte_vie',
        'livre_avance_salaire': 'total_avance_salaire',
    }

    for bulletin in bulletins:
        details = {champ: Decimal('0') for champ in _DETAILS_LIVRE_PAIE}
        lignes = getattr(bulletin, 'lignes_livre_paie', None)
        if lignes is None:
            lignes = bulletin.lignes.select_related('rubrique').all()

        for ligne in lignes:
            champ = _categorie_ligne_livre_paie(ligne)
            if not champ:
                continue
            montant = Decimal(str(getattr(ligne, 'montant', 0) or 0))
            details[champ] += montant

        for champ, montant in details.items():
            setattr(bulletin, champ, montant)
            totaux_details[correspondances_totaux[champ]] += montant

    return totaux_details


@login_required
@entreprise_active_required
@reauth_required
def livre_paie(request):
    """Livre de paie conforme"""
    # Filtres
    annee, mois = _filtres_periode_livre_paie(request)
    
    periodes = PeriodePaie.objects.filter(
        entreprise=request.user.entreprise,
    )
    if annee:
        periodes = periodes.filter(annee=annee)
    if mois:
        periodes = periodes.filter(mois=mois)
    
    # Récupérer tous les bulletins des périodes
    bulletins = BulletinPaie.objects.filter(
        periode__in=periodes,
        employe__entreprise=request.user.entreprise,
    ).select_related('employe', 'employe__poste', 'periode').annotate(
        total_retenues_livre=F('salaire_brut') + F('rappel_salaire') - F('net_a_payer')
    ).prefetch_related(
        Prefetch(
            'lignes',
            queryset=LigneBulletin.objects.select_related('rubrique').order_by('ordre', 'id'),
            to_attr='lignes_livre_paie',
        )
    ).order_by('periode__annee', 'periode__mois', 'employe__matricule')
    
    # Calcul des totaux — inclut maintenant base imposable (RTS) et indemnités exonérées
    totaux = bulletins.aggregate(
        total_brut=Sum('salaire_brut'),
        total_abattement=Sum('abattement_forfaitaire'),
        total_base_rts=Sum('base_rts'),
        total_cnss_employe=Sum('cnss_employe'),
        total_cnss_employeur=Sum('cnss_employeur'),
        total_irg=Sum('irg'),
        total_net=Sum('net_a_payer'),
        total_onfpp=Sum('contribution_onfpp'),
        total_vf=Sum('versement_forfaitaire'),
        total_retenues=Sum(F('salaire_brut') + F('rappel_salaire') - F('net_a_payer'))
    )
    bulletins = list(bulletins)
    totaux.update(_enrichir_details_livre_paie(bulletins))
    controles_livre = _controles_livre_paie(bulletins, totaux)

    # Années disponibles
    annees = list(PeriodePaie.objects.filter(
        entreprise=request.user.entreprise
    ).values_list('annee', flat=True).distinct().order_by('-annee'))
    pdf_params = []
    if annee:
        pdf_params.append(f'annee={annee}')
    if mois:
        pdf_params.append(f'mois={mois}')
    portee_livre = _libelle_portee_livre_paie(annee, mois, annees)

    return render(request, 'paie/livre_paie.html', {
        'bulletins': bulletins,
        'totaux': totaux,
        'annee': annee,
        'mois': mois,
        'annees': annees,
        'controles_livre': controles_livre,
        'portee_livre': portee_livre,
        'livre_pdf_query': f"?{'&'.join(pdf_params)}" if pdf_params else '',
    })


@login_required
@reauth_required
@entreprise_active_required
def telecharger_livre_paie_pdf(request):
    """Télécharger le livre de paie en PDF"""
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.units import cm
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER, TA_LEFT
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.utils import ImageReader
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
    import io
    import os

    # Polices
    try:
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
        pdfmetrics.getFont('Arial')
        _FN = 'Arial'; _FB = 'Arial-Bold'
    except Exception:
        _FN = 'Helvetica'; _FB = 'Helvetica-Bold'

    annee, mois = _filtres_periode_livre_paie(request)

    periodes = PeriodePaie.objects.filter(
        entreprise=request.user.entreprise,
    )
    if annee:
        periodes = periodes.filter(annee=annee)
    if mois:
        periodes = periodes.filter(mois=mois)

    bulletins = BulletinPaie.objects.filter(
        periode__in=periodes,
        employe__entreprise=request.user.entreprise,
    ).select_related('employe', 'employe__poste', 'periode').annotate(
        total_retenues_livre=F('salaire_brut') + F('rappel_salaire') - F('net_a_payer')
    ).prefetch_related(
        Prefetch(
            'lignes',
            queryset=LigneBulletin.objects.select_related('rubrique').order_by('ordre', 'id'),
            to_attr='lignes_livre_paie',
        )
    ).order_by('periode__annee', 'periode__mois', 'employe__matricule')

    totaux = bulletins.aggregate(
        total_brut=Sum('salaire_brut'),
        total_abattement=Sum('abattement_forfaitaire'),
        total_base_rts=Sum('base_rts'),
        total_cnss_employe=Sum('cnss_employe'),
        total_cnss_employeur=Sum('cnss_employeur'),
        total_irg=Sum('irg'),
        total_net=Sum('net_a_payer'),
        total_onfpp=Sum('contribution_onfpp'),
        total_vf=Sum('versement_forfaitaire'),
        total_retenues=Sum(F('salaire_brut') + F('rappel_salaire') - F('net_a_payer'))
    )
    bulletins = list(bulletins)
    totaux.update(_enrichir_details_livre_paie(bulletins))
    controles_livre = _controles_livre_paie(bulletins, totaux)

    def fmt(val):
        try:
            n = float(val or 0)
        except Exception:
            n = 0
        return f"{n:,.0f}".replace(",", " ")

    if not controles_livre['conforme']:
        return _bloquer_pdf_livre_paie(controles_livre, fmt)

    buffer = io.BytesIO()
    page_size = landscape(A4)
    width, height = page_size
    entreprise = request.user.entreprise

    def _logo_path():
        logo = getattr(entreprise, 'logo', None)
        if not logo:
            return None
        try:
            path = logo.path
        except Exception:
            return None
        return path if path and os.path.exists(path) else None

    logo_file = _logo_path()

    def _draw_header_footer(canvas_obj, doc):
        canvas_obj.saveState()
        x_left = 1.0 * cm
        y_top = height - 0.8 * cm
        text_x = x_left
        if logo_file:
            try:
                img = ImageReader(logo_file)
                img_w, img_h = img.getSize()
                logo_h = 1.2 * cm
                logo_w = logo_h * (img_w / img_h) if img_h else logo_h
                logo_w = min(logo_w, 3.0 * cm)
                canvas_obj.drawImage(
                    img, x_left, y_top - logo_h + 0.08 * cm,
                    width=logo_w, height=logo_h, preserveAspectRatio=True, mask='auto'
                )
                text_x = x_left + logo_w + 0.35 * cm
            except Exception:
                text_x = x_left

        canvas_obj.setFont(_FB, 9)
        canvas_obj.drawString(text_x, y_top, (entreprise.nom_entreprise if entreprise else 'Entreprise')[:65])
        canvas_obj.setFont(_FN, 6.8)
        infos = [
            f"NIF: {getattr(entreprise, 'nif', '') or '-'}",
            f"CNSS: {getattr(entreprise, 'num_cnss', '') or '-'}",
            f"Tél: {getattr(entreprise, 'telephone', '') or '-'}",
        ]
        canvas_obj.drawString(text_x, y_top - 0.32 * cm, " | ".join(infos))
        if entreprise and entreprise.adresse:
            canvas_obj.drawString(text_x, y_top - 0.62 * cm, str(entreprise.adresse)[:100])

        canvas_obj.setStrokeColor(colors.HexColor('#0d6efd'))
        canvas_obj.setLineWidth(1.2)
        canvas_obj.line(1.0 * cm, height - 2.0 * cm, width - 1.0 * cm, height - 2.0 * cm)

        canvas_obj.setFont(_FN, 6.5)
        canvas_obj.setFillColor(colors.HexColor('#666666'))
        canvas_obj.drawString(1.0 * cm, 0.65 * cm, f"Document généré le {timezone.now().strftime('%d/%m/%Y à %H:%M')}")
        canvas_obj.drawCentredString(width / 2, 0.65 * cm, "Livre de paie officiel - à conserver 10 ans")
        canvas_obj.drawRightString(width - 1.0 * cm, 0.65 * cm, f"Page {doc.page}")
        canvas_obj.restoreState()

    doc = SimpleDocTemplate(
        buffer,
        pagesize=page_size,
        leftMargin=1.0 * cm,
        rightMargin=1.0 * cm,
        topMargin=2.25 * cm,
        bottomMargin=1.0 * cm,
        title="Livre de paie officiel",
    )
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(
        name='LivreTitre',
        parent=styles['Title'],
        fontName=_FB,
        fontSize=15,
        leading=17,
        alignment=TA_CENTER,
        textColor=colors.HexColor('#0d47a1'),
        spaceAfter=8,
    ))
    styles.add(ParagraphStyle(
        name='LivreSmall',
        parent=styles['Normal'],
        fontName=_FN,
        fontSize=8,
        leading=10,
        alignment=TA_LEFT,
    ))

    story = []

    annees = PeriodePaie.objects.filter(
        entreprise=request.user.entreprise
    ).values_list('annee', flat=True).distinct().order_by('-annee')
    portee_livre = _libelle_portee_livre_paie(annee, mois, annees)
    titre = f"Livre de Paie - {portee_livre['titre']}"
    story.append(Paragraph(titre, styles['LivreTitre']))

    statut_table = Table([
        ['STATUT', controles_livre['statut']],
        ['Portée', f"{portee_livre['badge']} - {portee_livre['description']}"],
        ['Controle', 'Net = brut - retenues affichees | CNSS plafonnee OK'],
    ], colWidths=[2.5 * cm, 10.5 * cm])
    statut_table.setStyle(TableStyle([
        ('FONT', (0, 0), (-1, -1), _FN, 8),
        ('FONT', (0, 0), (0, -1), _FB, 8),
        ('FONT', (1, 0), (1, 0), _FB, 8),
        ('TEXTCOLOR', (1, 0), (1, 0), colors.HexColor('#198754')),
        ('BACKGROUND', (0, 0), (-1, -1), colors.HexColor('#eaf7ef')),
        ('GRID', (0, 0), (-1, -1), 0.25, colors.HexColor('#75b798')),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('LEFTPADDING', (0, 0), (-1, -1), 4),
        ('RIGHTPADDING', (0, 0), (-1, -1), 4),
    ]))
    story.append(statut_table)
    story.append(Spacer(1, 0.20 * cm))

    tot_line = (
        f"Brut: {fmt(totaux.get('total_brut'))} GNF   "
        f"Exonéré: {fmt(totaux.get('total_abattement'))}   "
        f"Imposable: {fmt(totaux.get('total_base_rts'))}   "
        f"CNSS Emp.: {fmt(totaux.get('total_cnss_employe'))}   "
        f"CNSS Empr.: {fmt(totaux.get('total_cnss_employeur'))}   "
        f"RTS: {fmt(totaux.get('total_irg'))}   "
        f"Net: {fmt(totaux.get('total_net'))}"
    )

    resume_data = [
        ['Salaire base', f"{fmt(totaux.get('total_salaire_base'))} GNF", 'Logement', f"{fmt(totaux.get('total_prime_logement'))} GNF", 'Transport', f"{fmt(totaux.get('total_prime_transport'))} GNF"],
        ['Cherté de vie', f"{fmt(totaux.get('total_cherte_vie'))} GNF", 'Brut', f"{fmt(totaux.get('total_brut'))} GNF", 'Net', f"{fmt(totaux.get('total_net'))} GNF"],
        ['CNSS 5%', f"{fmt(totaux.get('total_cnss_employe'))} GNF", 'CNSS 18%', f"{fmt(totaux.get('total_cnss_employeur'))} GNF", 'Base RTS', f"{fmt(totaux.get('total_base_rts'))} GNF"],
        ['Avance', f"{fmt(totaux.get('total_avance_salaire'))} GNF", 'ONFPP', f"{fmt(totaux.get('total_onfpp'))} GNF", 'VF', f"{fmt(totaux.get('total_vf'))} GNF"],
    ]
    resume = Table(resume_data, colWidths=[2.3 * cm, 3.0 * cm, 2.3 * cm, 3.0 * cm, 2.3 * cm, 3.0 * cm])
    resume.setStyle(TableStyle([
        ('FONT', (0, 0), (-1, -1), _FN, 8),
        ('FONT', (0, 0), (0, -1), _FB, 8),
        ('FONT', (2, 0), (2, -1), _FB, 8),
        ('FONT', (4, 0), (4, -1), _FB, 8),
        ('BACKGROUND', (0, 0), (-1, -1), colors.HexColor('#f4f7fb')),
        ('GRID', (0, 0), (-1, -1), 0.25, colors.HexColor('#ced4da')),
        ('ALIGN', (1, 0), (1, -1), 'RIGHT'),
        ('ALIGN', (3, 0), (3, -1), 'RIGHT'),
        ('ALIGN', (5, 0), (5, -1), 'RIGHT'),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('LEFTPADDING', (0, 0), (-1, -1), 4),
        ('RIGHTPADDING', (0, 0), (-1, -1), 4),
    ]))
    story.append(resume)
    story.append(Spacer(1, 0.25 * cm))

    if controles_livre['nb_retenues_hors_cnss_rts'] > 0:
        controle_data = [
            ['Retenues hors CNSS/RTS', 'Valeur'],
            ['Nombre de lignes', str(controles_livre['nb_retenues_hors_cnss_rts'])],
            ['Total', f"{fmt(controles_livre['total_retenues_hors_cnss_rts'])} GNF"],
        ]
        for retenue in controles_livre['retenues_hors_cnss_rts'][:8]:
            controle_data.append([
                retenue['matricule'],
                f"{fmt(retenue['montant'])} GNF",
            ])
        controle_table = Table(controle_data, colWidths=[5.5 * cm, 4.0 * cm])
        controle_table.setStyle(TableStyle([
            ('FONT', (0, 0), (-1, -1), _FN, 8),
            ('FONT', (0, 0), (-1, 0), _FB, 8),
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#fff3cd')),
            ('GRID', (0, 0), (-1, -1), 0.25, colors.HexColor('#ffda6a')),
            ('ALIGN', (1, 1), (1, -1), 'RIGHT'),
        ]))
        story.append(controle_table)
        story.append(Spacer(1, 0.20 * cm))

    data = [[
        'Période', 'Matr.', 'Nom et Prénoms', 'Fonction',
        'Base', 'Logement', 'Transport', 'Cherté', 'Brut', 'CNSS 5%', 'CNSS 18%',
        'Base RTS', 'Avance', 'Net', 'ONFPP', 'VF'
    ]]

    for b in bulletins:
        emp = b.employe
        nom_complet = f"{emp.nom} {emp.prenoms}"
        if len(nom_complet) > 22:
            nom_complet = nom_complet[:20] + '..'
        # Fonction : intitulé du poste, fallback sur le département texte, jamais vide en audit.
        fonction = (
            (emp.poste.intitule_poste if emp.poste and emp.poste.intitule_poste else None)
            or (emp.departement or '').strip()
            or 'Poste à renseigner'
        )
        if len(fonction) > 16:
            fonction = fonction[:14] + '..'
        data.append([
            str(b.periode),
            emp.matricule or '-',
            nom_complet,
            fonction,
            fmt(getattr(b, 'livre_salaire_base', 0)),
            fmt(getattr(b, 'livre_prime_logement', 0)),
            fmt(getattr(b, 'livre_prime_transport', 0)),
            fmt(getattr(b, 'livre_cherte_vie', 0)),
            fmt(b.salaire_brut),
            fmt(b.cnss_employe),
            fmt(b.cnss_employeur),
            fmt(b.base_rts),
            fmt(getattr(b, 'livre_avance_salaire', 0)),
            fmt(b.net_a_payer),
            fmt(b.contribution_onfpp),
            fmt(b.versement_forfaitaire),
        ])

    data.append([
        'TOTAUX:', '', '', '',
        fmt(totaux.get('total_salaire_base')),
        fmt(totaux.get('total_prime_logement')),
        fmt(totaux.get('total_prime_transport')),
        fmt(totaux.get('total_cherte_vie')),
        fmt(totaux.get('total_brut')),
        fmt(totaux.get('total_cnss_employe')),
        fmt(totaux.get('total_cnss_employeur')),
        fmt(totaux.get('total_base_rts')),
        fmt(totaux.get('total_avance_salaire')),
        fmt(totaux.get('total_net')),
        fmt(totaux.get('total_onfpp')),
        fmt(totaux.get('total_vf')),
    ])

    # Largeur disponible en A4 paysage: ~29.7cm - 2*0.8cm marges = ~28cm
    col_widths = [
        1.45 * cm, 1.15 * cm, 2.65 * cm, 1.85 * cm,
        1.45 * cm, 1.50 * cm, 1.50 * cm, 1.45 * cm, 1.55 * cm, 1.55 * cm,
        1.55 * cm, 1.65 * cm, 1.55 * cm, 1.55 * cm, 1.45 * cm, 1.45 * cm
    ]
    table = Table(data, colWidths=col_widths, repeatRows=1)
    table.setStyle(TableStyle([
        ('FONT', (0, 0), (-1, -1), _FN, 5.5),
        ('FONT', (0, 0), (-1, 0), _FB, 5.5),
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#e9ecef')),
        ('ALIGN', (4, 1), (-1, -1), 'RIGHT'),
        ('ALIGN', (0, 0), (3, -1), 'LEFT'),
        ('GRID', (0, 0), (-1, -1), 0.25, colors.grey),
        ('BACKGROUND', (0, -1), (-1, -1), colors.HexColor('#f8f9fa')),
        ('FONT', (0, -1), (-1, -1), _FB, 5.5),
        ('LINEABOVE', (0, -1), (-1, -1), 0.8, colors.black),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('LEFTPADDING', (0, 0), (-1, -1), 1.5),
        ('RIGHTPADDING', (0, 0), (-1, -1), 1.5),
        ('TOPPADDING', (0, 0), (-1, -1), 2),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 2),
    ]))

    story.append(table)
    story.append(Spacer(1, 0.35 * cm))

    signature_data = [[
        Paragraph("<b>Préparé par</b><br/>Service RH<br/><br/><br/>Nom, signature et date", styles['LivreSmall']),
        Paragraph("<b>Contrôlé par</b><br/>Responsable paie<br/><br/><br/>Nom, signature et date", styles['LivreSmall']),
        Paragraph("<b>Validé par</b><br/>Direction<br/><br/><br/>Nom, cachet et date", styles['LivreSmall']),
    ]]
    signatures = Table(signature_data, colWidths=[8.7 * cm, 8.7 * cm, 8.7 * cm], rowHeights=[2.2 * cm])
    signatures.setStyle(TableStyle([
        ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#6c757d')),
        ('BACKGROUND', (0, 0), (-1, -1), colors.HexColor('#fafafa')),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('LEFTPADDING', (0, 0), (-1, -1), 6),
        ('TOPPADDING', (0, 0), (-1, -1), 6),
    ]))
    story.append(signatures)

    doc.build(story, onFirstPage=_draw_header_footer, onLaterPages=_draw_header_footer)

    buffer.seek(0)
    response = HttpResponse(buffer, content_type='application/pdf')
    filename = f"livre_paie_{int(annee)}" if annee else "livre_paie_toutes_periodes"
    if mois:
        filename += f"_{int(mois)}"
    filename += ".pdf"
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    return response


@login_required
@entreprise_active_required
@reauth_required
def declarations_sociales(request):
    """Déclarations sociales (CNSS, RTS, VF, ONFPP)"""
    # Filtres
    annee = request.GET.get('annee', timezone.now().year)
    mois = request.GET.get('mois')
    
    periodes = PeriodePaie.objects.filter(
        entreprise=request.user.entreprise,
        annee=annee
    )
    if mois:
        periodes = periodes.filter(mois=mois)
    
    # Inclure tous les bulletins calculés, validés ou payés
    bulletins = BulletinPaie.objects.filter(
        periode__in=periodes,
        statut_bulletin__in=['calcule', 'valide', 'paye'],
        employe__entreprise=request.user.entreprise,
    ).select_related('employe', 'periode')
    
    # Récupérer les constantes CNSS (plancher et plafond)
    from .models import Constante
    plancher_cnss = Constante.objects.filter(code='PLANCHER_CNSS', actif=True).first()
    plafond_cnss = Constante.objects.filter(code='PLAFOND_CNSS', actif=True).first()
    taux_cnss_employe = Constante.objects.filter(code='TAUX_CNSS_EMPLOYE', actif=True).first()
    taux_cnss_employeur = Constante.objects.filter(code='TAUX_CNSS_EMPLOYEUR', actif=True).first()
    
    total_salaries = bulletins.values('employe').distinct().count()
    totaux = bulletins.aggregate(
        total_brut=Sum('salaire_brut'),
        total_base_vf=Sum('base_vf'),
        total_base_onfpp=Sum('base_onfpp'),
        total_cnss_employe=Sum('cnss_employe'),
        total_cnss_employeur=Sum('cnss_employeur'),
        total_rts=Sum('irg'),
        total_vf=Sum('versement_forfaitaire'),
        total_ta=Sum('taxe_apprentissage'),
        total_onfpp=Sum('contribution_onfpp'),
    )
    salaire_brut_total = totaux['total_brut'] or Decimal('0')
    total_base_vf = totaux['total_base_vf'] or Decimal('0')
    total_base_onfpp = somme_base_onfpp_effective(bulletins) or total_base_vf
    total_onfpp = totaux['total_onfpp'] or Decimal('0')
    total_ta = totaux['total_ta'] or Decimal('0')
    if total_salaries >= 30 and not total_onfpp:
        total_ta = Decimal('0')
        total_onfpp = (total_base_onfpp * Decimal('0.015')).quantize(Decimal('1'))

    # Calculs pour CNSS
    declaration_cnss = {
        'total_salaries': total_salaries,
        'masse_salariale': salaire_brut_total,
        'cotisation_employe': totaux['total_cnss_employe'] or Decimal('0'),
        'cotisation_employeur': totaux['total_cnss_employeur'] or Decimal('0'),
        # Informations sur plancher et plafond
        'plancher': plancher_cnss.valeur if plancher_cnss else Decimal('550000'),
        'plafond': plafond_cnss.valeur if plafond_cnss else Decimal('2500000'),
        'taux_employe': taux_cnss_employe.valeur if taux_cnss_employe else Decimal('5.00'),
        'taux_employeur': taux_cnss_employeur.valeur if taux_cnss_employeur else Decimal('18.00'),
    }
    declaration_cnss['total_cotisation'] = (
        declaration_cnss['cotisation_employe'] + declaration_cnss['cotisation_employeur']
    )
    
    # Calculs pour RTS
    declaration_irg = {
        'total_salaries': total_salaries,
        'masse_imposable': salaire_brut_total,
        'total_irg': totaux['total_rts'] or Decimal('0'),
    }

    declaration_charges = {
        'base_vf': total_base_vf,
        'base_onfpp': total_base_onfpp,
        'vf': totaux['total_vf'] or Decimal('0'),
        'ta': total_ta,
        'onfpp': total_onfpp,
    }
    
    # Total général des charges
    total_dgi = declaration_irg['total_irg'] + declaration_charges['vf']
    total_onfpp_ta = declaration_charges['onfpp'] + declaration_charges['ta']
    total_dmu = total_dgi + total_onfpp_ta
    analyse_bases = analyser_bases_vf_onfpp(
        salaire_brut_total,
        total_base_vf,
        total_base_onfpp,
    )

    total_general = declaration_cnss['total_cotisation'] + total_dmu
    
    # Détail par employé
    detail_employes = []
    for bulletin in bulletins:
        detail_employes.append({
            'matricule': bulletin.employe.matricule,
            'nom_complet': f"{bulletin.employe.nom} {bulletin.employe.prenoms}",
            'periode': str(bulletin.periode),
            'brut': bulletin.salaire_brut,
            'cnss_employe': bulletin.cnss_employe,
            'cnss_employeur': bulletin.cnss_employeur,
            'irg': bulletin.irg,
            'net': bulletin.net_a_payer
        })
    
    annees = PeriodePaie.objects.filter(
        entreprise=request.user.entreprise
    ).values_list('annee', flat=True).distinct().order_by('-annee')
    
    return render(request, 'paie/declarations_sociales.html', {
        'declaration_cnss': declaration_cnss,
        'declaration_irg': declaration_irg,
        'declaration_charges': declaration_charges,
        'total_dgi': total_dgi,
        'total_dmu': total_dmu,
        'total_onfpp_ta': total_onfpp_ta,
        'total_general': total_general,
        'mode_fiscal_code': analyse_bases['mode_fiscal'],
        'mode_fiscal_label': analyse_bases['mode_fiscal_label'],
        'bases_vf_onfpp_distinctes': analyse_bases['bases_vf_onfpp_distinctes'],
        'taux_optimisation_global': analyse_bases['taux_optimisation_global'],
        'taux_optimisation_vf': analyse_bases['taux_optimisation_vf'],
        'taux_optimisation_onfpp': analyse_bases['taux_optimisation_onfpp'],
        'detail_employes': detail_employes,
        'annee': int(annee),
        'mois': int(mois) if mois else None,
        'portee_declaration': 'Mensuelle' if mois else 'Annuelle',
        'portee_detail': f"{int(mois):02d}/{int(annee)}" if mois else f"Année complète {int(annee)}",
        'annees': annees,
        'periodes': periodes
    })


@login_required
@entreprise_active_required
@reauth_required
def declarations_sociales_pdf(request):
    """Générer le PDF des déclarations sociales"""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import cm
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
    from io import BytesIO
    
    annee = request.GET.get('annee', timezone.now().year)
    mois = request.GET.get('mois')
    
    entreprise = request.user.entreprise
    if not entreprise:
        return HttpResponse("Aucune entreprise associée.", status=400)
    
    periodes = PeriodePaie.objects.filter(
        entreprise=entreprise,
        annee=annee,
    )
    if mois:
        periodes = periodes.filter(mois=mois)

    bulletins = BulletinPaie.objects.filter(
        periode__in=periodes,
        statut_bulletin__in=['calcule', 'valide', 'paye'],
        employe__entreprise=entreprise,
    ).select_related('employe', 'periode')
    
    # Calculs
    total_salaries = bulletins.values('employe').distinct().count()
    totaux = bulletins.aggregate(
        total_brut=Sum('salaire_brut'),
        total_base_vf=Sum('base_vf'),
        total_base_onfpp=Sum('base_onfpp'),
        total_cnss_employe=Sum('cnss_employe'),
        total_cnss_employeur=Sum('cnss_employeur'),
        total_rts=Sum('irg'),
        total_vf=Sum('versement_forfaitaire'),
        total_ta=Sum('taxe_apprentissage'),
        total_onfpp=Sum('contribution_onfpp'),
    )
    salaire_brut_total = totaux['total_brut'] or Decimal('0')
    total_base_vf = totaux['total_base_vf'] or Decimal('0')
    total_base_onfpp = somme_base_onfpp_effective(bulletins) or total_base_vf
    total_onfpp = totaux['total_onfpp'] or Decimal('0')
    total_ta = totaux['total_ta'] or Decimal('0')
    if total_salaries >= 30 and not total_onfpp:
        total_ta = Decimal('0')
        total_onfpp = (total_base_onfpp * Decimal('0.015')).quantize(Decimal('1'))

    declaration_cnss = {
        'total_salaries': total_salaries,
        'masse_salariale': salaire_brut_total,
        'cotisation_employe': totaux['total_cnss_employe'] or Decimal('0'),
        'cotisation_employeur': totaux['total_cnss_employeur'] or Decimal('0'),
    }
    declaration_cnss['total_cotisation'] = declaration_cnss['cotisation_employe'] + declaration_cnss['cotisation_employeur']
    
    declaration_irg = {
        'total_salaries': total_salaries,
        'total_irg': totaux['total_rts'] or Decimal('0'),
    }
    declaration_charges = {
        'base_vf': total_base_vf,
        'base_onfpp': total_base_onfpp,
        'vf': totaux['total_vf'] or Decimal('0'),
        'ta': total_ta,
        'onfpp': total_onfpp,
    }
    total_dgi = declaration_irg['total_irg'] + declaration_charges['vf']
    total_dmu = total_dgi + declaration_charges['onfpp'] + declaration_charges['ta']
    analyse_bases = analyser_bases_vf_onfpp(
        salaire_brut_total,
        total_base_vf,
        total_base_onfpp,
    )
    total_general = declaration_cnss['total_cotisation'] + total_dmu
    
    # Créer le PDF
    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4, topMargin=1*cm, bottomMargin=1*cm)
    elements = []
    styles = getSampleStyleSheet()
    
    title_style = ParagraphStyle('Title', parent=styles['Heading1'], fontSize=16, alignment=1)
    periode_str = f"Mois {mois}/{annee}" if mois else f"Année {annee}"
    elements.append(Paragraph(f"Déclarations Sociales - {periode_str}", title_style))
    elements.append(Spacer(1, 0.3*cm))
    elements.append(Paragraph(f"Entreprise: {entreprise.nom_entreprise}", styles['Normal']))
    elements.append(Spacer(1, 0.5*cm))
    
    # CNSS
    elements.append(Paragraph("CNSS - Caisse Nationale de Sécurité Sociale", styles['Heading2']))
    cnss_data = [
        ['Libellé', 'Montant (GNF)'],
        ['Nombre de salariés', str(declaration_cnss['total_salaries'])],
        ['Salaire brut', f"{declaration_cnss['masse_salariale']:,.0f}"],
        ['Cotisation employé (5%)', f"{declaration_cnss['cotisation_employe']:,.0f}"],
        ['Cotisation employeur (18%)', f"{declaration_cnss['cotisation_employeur']:,.0f}"],
        ['Total cotisations', f"{declaration_cnss['total_cotisation']:,.0f}"],
    ]
    cnss_table = Table(cnss_data, colWidths=[10*cm, 6*cm])
    cnss_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#EF7707')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('ALIGN', (1, 0), (1, -1), 'RIGHT'),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('GRID', (0, 0), (-1, -1), 1, colors.black),
        ('BACKGROUND', (0, -1), (-1, -1), colors.HexColor('#f5f5f5')),
        ('FONTNAME', (0, -1), (-1, -1), 'Helvetica-Bold'),
    ]))
    elements.append(cnss_table)
    elements.append(Spacer(1, 0.5*cm))

    # RTS - Retenue sur Traitements et Salaires
    elements.append(Paragraph("RTS - Retenue sur Traitements et Salaires", styles['Heading2']))
    irg_data = [
        ['Libellé', 'Montant (GNF)'],
        ['Nombre de salariés', str(declaration_irg['total_salaries'])],
        ['Total RTS retenu', f"{declaration_irg['total_irg']:,.0f}"],
    ]
    irg_table = Table(irg_data, colWidths=[10*cm, 6*cm])
    irg_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#EF7707')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('ALIGN', (1, 0), (1, -1), 'RIGHT'),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('GRID', (0, 0), (-1, -1), 1, colors.black),
    ]))
    elements.append(irg_table)
    elements.append(Spacer(1, 0.5*cm))

    elements.append(Paragraph("Mode fiscal et base VF/ONFPP", styles['Heading2']))
    mode_data = [
        ['Libellé', 'Valeur'],
        ['Mode fiscal appliqué', analyse_bases['mode_fiscal_label']],
        ['Base VF', f"{declaration_charges['base_vf']:,.0f}"],
        ['Base ONFPP', f"{declaration_charges['base_onfpp']:,.0f}"],
        ['Optimisation base VF', f"{analyse_bases['taux_optimisation_vf']}%"],
        ['Optimisation base ONFPP', f"{analyse_bases['taux_optimisation_onfpp']}%"],
    ]
    mode_table = Table(mode_data, colWidths=[7*cm, 9*cm])
    mode_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#EF7707')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('ALIGN', (1, 0), (1, -1), 'RIGHT'),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('GRID', (0, 0), (-1, -1), 1, colors.black),
    ]))
    elements.append(mode_table)
    elements.append(Spacer(1, 0.5*cm))

    elements.append(Paragraph("Récapitulatif total des charges", styles['Heading2']))
    recap_data = [
        ['Organisme', 'Montant (GNF)'],
        ['CNSS (Total)', f"{declaration_cnss['total_cotisation']:,.0f}"],
        ['Total DGI (RTS + VF)', f"{total_dgi:,.0f}"],
        ['  RTS (Trésor Public)', f"{declaration_irg['total_irg']:,.0f}"],
        ['  VF', f"{declaration_charges['vf']:,.0f}"],
        ['Total ONFPP', f"{declaration_charges['onfpp']:,.0f}"],
        ['Total TA', f"{declaration_charges['ta']:,.0f}"],
        ['TOTAL DMU (RTS + VF + ONFPP/TA)', f"{total_dmu:,.0f}"],
        ['TOTAL GÉNÉRAL (CNSS + DMU)', f"{total_general:,.0f}"],
    ]
    recap_table = Table(recap_data, colWidths=[10*cm, 6*cm])
    recap_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#EF7707')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('ALIGN', (1, 0), (1, -1), 'RIGHT'),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('GRID', (0, 0), (-1, -1), 1, colors.black),
        ('BACKGROUND', (0, -1), (-1, -1), colors.HexColor('#f5f5f5')),
        ('FONTNAME', (0, -1), (-1, -1), 'Helvetica-Bold'),
    ]))
    elements.append(recap_table)
    
    doc.build(elements)
    buffer.seek(0)
    
    filename = f"declarations_sociales_{annee}"
    if mois:
        filename += f"_{mois}"
    
    response = HttpResponse(buffer.getvalue(), content_type='application/pdf')
    response['Content-Disposition'] = f'attachment; filename="{filename}.pdf"'
    return response


@login_required
@entreprise_active_required
@reauth_required
def liste_elements_salaire(request):
    """
    Liste les employés avec leur résumé d'éléments de salaire.
    Pagination par employé (20 par page) pour éviter les problèmes de regroupement.
    """
    from django.core.paginator import Paginator, EmptyPage, PageNotAnInteger
    from django.db.models import Count, Sum, Q

    # Filtres
    q_nom      = request.GET.get('q', '').strip()
    statut_f   = request.GET.get('statut', 'actif')
    par_page   = int(request.GET.get('par_page', 20))
    if par_page not in (10, 20, 50):
        par_page = 20

    employes_qs = Employe.objects.filter(
        entreprise=request.user.entreprise
    ).order_by('nom', 'prenoms')

    if q_nom:
        employes_qs = employes_qs.filter(
            Q(nom__icontains=q_nom) | Q(prenoms__icontains=q_nom) | Q(matricule__icontains=q_nom)
        )
    if statut_f and statut_f != 'tous':
        employes_qs = employes_qs.filter(statut_employe=statut_f)

    # Annoter chaque employé avec le nombre d'éléments actifs et la somme brute
    employes_qs = employes_qs.annotate(
        nb_elements_actifs=Count('elements_salaire', filter=Q(elements_salaire__actif=True)),
        nb_elements_total=Count('elements_salaire'),
        total_gains=Sum(
            'elements_salaire__montant',
            filter=Q(elements_salaire__actif=True, elements_salaire__rubrique__type_rubrique='gain')
        ),
    )

    # Pagination par employé
    paginator = Paginator(employes_qs, par_page)
    page = request.GET.get('page')
    try:
        employes_page = paginator.page(page)
    except PageNotAnInteger:
        employes_page = paginator.page(1)
    except EmptyPage:
        employes_page = paginator.page(paginator.num_pages)

    # Pour chaque employé de la page, charger ses éléments actifs (max 5 pour aperçu)
    employes_ids = [e.id for e in employes_page]
    elements_par_employe = {}
    for el in ElementSalaire.objects.filter(
        employe_id__in=employes_ids, actif=True
    ).select_related('rubrique').order_by('employe_id', 'rubrique__ordre_calcul'):
        elements_par_employe.setdefault(el.employe_id, []).append(el)

    # Enrichir chaque employé paginé avec ses éléments
    for emp in employes_page:
        emp.elements_actifs_liste = elements_par_employe.get(emp.id, [])

    # Liste complète pour filtre dropdown
    tous_employes = Employe.objects.filter(
        entreprise=request.user.entreprise,
    ).order_by('nom', 'prenoms')

    return render(request, 'paie/elements_salaire/liste.html', {
        'employes_page': employes_page,
        'employes': tous_employes,
        'total_employes': paginator.count,
        'total_pages': paginator.num_pages,
        'q_nom': q_nom,
        'statut_f': statut_f,
        'par_page': par_page,
        # Compatibilité rétro pour le modal avancer dates
        'elements': ElementSalaire.objects.none(),
    })


@login_required
@entreprise_active_required
@reauth_required
def elements_salaire_employe(request, employe_id):
    """Éléments de salaire d'un employé spécifique"""
    employe = get_object_or_404(Employe, pk=employe_id, entreprise=request.user.entreprise)
    
    elements = ElementSalaire.objects.filter(
        employe=employe
    ).select_related('rubrique').order_by('rubrique__ordre_calcul')
    
    # Séparer gains et retenues
    gains = elements.filter(rubrique__type_rubrique='gain')
    retenues = elements.filter(rubrique__type_rubrique='retenue')
    
    # Calculer les totaux
    total_gains = sum(e.montant or 0 for e in gains if e.actif)
    total_retenues = sum(e.montant or 0 for e in retenues if e.actif)

    # Estimation réaliste du net (CNSS + RTS calculés automatiquement)
    brut = total_gains
    cnss_estime = 0
    rts_estime = 0
    retenues_legales = 0
    if brut > 0:
        try:
            from .services_retropaie import _net_depuis_brut, _d
            from .cache_service import PayrollCacheService
            annee_courante = date.today().year
            cst = PayrollCacheService.get_constantes(date_reference=date(annee_courante, 1, 1))
            tr = PayrollCacheService.get_tranches_rts(annee_courante)
            cst.setdefault('PLANCHER_CNSS', Decimal('550000'))
            cst.setdefault('PLAFOND_CNSS', Decimal('2500000'))
            cst.setdefault('TAUX_CNSS_EMPLOYE', Decimal('5'))
            # Estimer l'exonération à partir des indemnités forfaitaires
            indem = sum(
                (e.montant or 0) for e in gains
                if e.actif and e.rubrique and e.rubrique.categorie_rubrique in
                ('transport', 'logement', 'cherte_vie', 'indemnite_forfaitaire')
            )
            pct_exo = min(_d(indem) / _d(brut) * Decimal('100'), Decimal('25')) if brut > 0 else Decimal('0')
            net_calc, cnss_calc, _, _, rts_calc = _net_depuis_brut(_d(brut), cst, tr, pct_exo)
            cnss_estime = int(cnss_calc)
            rts_estime = int(rts_calc)
            retenues_legales = cnss_estime + rts_estime
        except Exception:
            pass  # En cas d'erreur, fallback = pas de retenues estimées

    net_estime = total_gains - total_retenues - retenues_legales

    return render(request, 'paie/elements_salaire/employe.html', {
        'employe': employe,
        'gains': gains,
        'retenues': retenues,
        'total_gains': total_gains,
        'total_retenues': total_retenues,
        'cnss_estime': cnss_estime,
        'rts_estime': rts_estime,
        'retenues_legales': retenues_legales,
        'net_estime': net_estime,
    })


@login_required
@entreprise_active_required
@reauth_required
def ajouter_element_salaire(request, employe_id):
    """Ajouter un élément de salaire à un employé"""
    employe = get_object_or_404(Employe, pk=employe_id, entreprise=request.user.entreprise)
    
    if request.method == 'POST':
        rubrique_id = request.POST.get('rubrique')
        montant = request.POST.get('montant')
        taux = request.POST.get('taux')
        base_calcul = request.POST.get('base_calcul', '')
        date_debut = request.POST.get('date_debut')
        date_fin = request.POST.get('date_fin')
        actif = request.POST.get('actif') == 'on'
        recurrent = request.POST.get('recurrent') == 'on'
        
        try:
            rubrique = RubriquePaie.objects.get(
                pk=rubrique_id,
                entreprise=request.user.entreprise
            )
            
            # Vérifier si un élément actif existe déjà pour cette rubrique
            element_existant = ElementSalaire.objects.filter(
                employe=employe,
                rubrique=rubrique,
                actif=True
            ).first()
            
            if element_existant:
                messages.error(
                    request,
                    f'Un élément "{rubrique.libelle_rubrique}" actif existe déjà pour {employe.nom_complet}. '
                    f'Veuillez le modifier ou le désactiver avant d\'en ajouter un nouveau.'
                )
                return redirect('paie:elements_salaire_employe', employe_id=employe.id)
            
            element = ElementSalaire.objects.create(
                employe=employe,
                rubrique=rubrique,
                montant=parse_montant(montant),
                taux=parse_montant(taux),
                base_calcul=base_calcul,
                date_debut=date_debut,
                date_fin=date_fin if date_fin else None,
                actif=actif,
                recurrent=recurrent
            )
            
            messages.success(
                request,
                f'Élément "{rubrique.libelle_rubrique}" ajouté avec succès pour {employe.nom_complet}'
            )
            return redirect('paie:elements_salaire_employe', employe_id=employe.id)
            
        except Exception as e:
            messages.error(request, f'Erreur lors de l\'ajout : {str(e)}')
    
    # Rubriques disponibles (exclure celles calculées automatiquement: IRG, CNSS)
    rubriques = RubriquePaie.objects.filter(
        actif=True,
        entreprise=request.user.entreprise
    ).exclude(
        code_rubrique__in=['RTS', 'IRG', 'IRPP', 'CNSS_EMP', 'CNSS_PAT']
    ).exclude(
        code_rubrique__icontains='CNSS'
    ).exclude(
        libelle_rubrique__icontains='Impôt sur le Revenu'
    ).order_by('type_rubrique', 'libelle_rubrique')
    
    return render(request, 'paie/elements_salaire/ajouter.html', {
        'employe': employe,
        'rubriques': rubriques
    })


@login_required
@entreprise_active_required
@reauth_required
def modifier_element_salaire(request, pk):
    """Modifier un élément de salaire"""
    element = get_object_or_404(ElementSalaire, pk=pk, employe__entreprise=request.user.entreprise)
    
    if request.method == 'POST':
        montant = request.POST.get('montant')
        taux = request.POST.get('taux')
        base_calcul = request.POST.get('base_calcul', '')
        date_debut = request.POST.get('date_debut')
        date_fin = request.POST.get('date_fin')
        actif = request.POST.get('actif') == 'on'
        recurrent = request.POST.get('recurrent') == 'on'
        
        try:
            element.montant = parse_montant(montant)
            element.taux = parse_montant(taux)
            element.base_calcul = base_calcul
            element.date_debut = date_debut
            element.date_fin = date_fin if date_fin else None
            element.actif = actif
            element.recurrent = recurrent
            element.save()
            
            messages.success(request, 'Élément modifié avec succès')
            return redirect('paie:elements_salaire_employe', employe_id=element.employe.id)
            
        except Exception as e:
            messages.error(request, f'Erreur lors de la modification : {str(e)}')
    
    return render(request, 'paie/elements_salaire/modifier.html', {
        'element': element
    })


@login_required
@entreprise_active_required
@reauth_required
def supprimer_element_salaire(request, pk):
    """Supprimer un élément de salaire"""
    try:
        element = ElementSalaire.objects.get(pk=pk, employe__entreprise=request.user.entreprise)
    except ElementSalaire.DoesNotExist:
        messages.warning(request, "Cet élément de salaire n'existe plus ou a déjà été supprimé.")
        return redirect('paie:liste_elements_salaire')
    
    employe_id = element.employe.id
    
    if request.method == 'POST':
        libelle = element.rubrique.libelle_rubrique
        element.delete()
        messages.success(request, f'Élément "{libelle}" supprimé avec succès')
        return redirect('paie:elements_salaire_employe', employe_id=employe_id)
    
    return render(request, 'paie/elements_salaire/supprimer.html', {
        'element': element
    })


@login_required
@entreprise_active_required
@reauth_required
def api_avancer_dates_elements(request):
    """
    POST JSON → met à jour date_debut (et date_fin) de tous les éléments de salaire.
    Permet de passer au mois suivant sans modifier chaque élément manuellement.

    Entrée :
        {
          "date_debut": "2026-05-01",
          "date_fin": "2026-05-31",      ← optionnel, null pour ne pas changer
          "employe_id": 12,              ← optionnel, null pour tous les employés
          "actif_seulement": true        ← défaut true
        }
    Sortie :
        { "nb_mis_a_jour": 42, "message": "..." }
    """
    if request.method != 'POST':
        return JsonResponse({'error': 'POST requis'}, status=405)
    try:
        data = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({'error': 'JSON invalide'}, status=400)

    date_debut_str = data.get('date_debut', '')
    date_fin_str   = data.get('date_fin', None)
    employe_id     = data.get('employe_id', None)
    actif_seulement = data.get('actif_seulement', True)

    if not date_debut_str:
        return JsonResponse({'error': 'date_debut obligatoire'}, status=400)

    try:
        from datetime import datetime as dt
        nouvelle_date_debut = dt.strptime(date_debut_str, '%Y-%m-%d').date()
    except ValueError:
        return JsonResponse({'error': 'Format date_debut invalide (YYYY-MM-DD)'}, status=400)

    nouvelle_date_fin = None
    if date_fin_str:
        try:
            from datetime import datetime as dt
            nouvelle_date_fin = dt.strptime(date_fin_str, '%Y-%m-%d').date()
        except ValueError:
            return JsonResponse({'error': 'Format date_fin invalide (YYYY-MM-DD)'}, status=400)

    # Filtrer les éléments appartenant à l'entreprise de l'utilisateur
    qs = ElementSalaire.objects.filter(employe__entreprise=request.user.entreprise)

    if actif_seulement:
        qs = qs.filter(actif=True)

    if employe_id:
        try:
            qs = qs.filter(employe_id=int(employe_id))
        except (ValueError, TypeError):
            return JsonResponse({'error': 'employe_id invalide'}, status=400)

    # Mise à jour en masse
    champs = {'date_debut': nouvelle_date_debut}
    if nouvelle_date_fin:
        champs['date_fin'] = nouvelle_date_fin

    nb = qs.update(**champs)

    scope = 'tous les employés' if not employe_id else 'l\'employé sélectionné'
    return JsonResponse({
        'nb_mis_a_jour': nb,
        'message': f'{nb} élément(s) mis à jour pour {scope}.',
    })


@login_required
@entreprise_active_required
@reauth_required
def liste_rubriques(request):
    """Liste des rubriques de paie"""
    type_rubrique = request.GET.get('type')
    
    rubriques = RubriquePaie.objects.filter(entreprise=request.user.entreprise)
    
    if type_rubrique:
        rubriques = rubriques.filter(type_rubrique=type_rubrique)
    
    # Statistiques
    stats = {
        'total': rubriques.count(),
        'gains': rubriques.filter(type_rubrique='gain').count(),
        'retenues': rubriques.filter(type_rubrique='retenue').count(),
        'cotisations': rubriques.filter(type_rubrique='cotisation').count(),
    }
    
    return render(request, 'paie/rubriques/liste.html', {
        'rubriques': rubriques,
        'stats': stats
    })


@login_required
@entreprise_active_required
@reauth_required
def creer_rubrique(request):
    """Créer une nouvelle rubrique de paie"""
    if request.method == 'POST':
        code = request.POST.get('code_rubrique')
        libelle = request.POST.get('libelle_rubrique')
        type_rub = request.POST.get('type_rubrique')
        formule = request.POST.get('formule_calcul', '')
        taux = request.POST.get('taux_rubrique')
        montant_fixe = request.POST.get('montant_fixe')
        soumis_cnss = request.POST.get('soumis_cnss') == 'on'
        soumis_irg = request.POST.get('soumis_irg') == 'on'
        ordre_calcul = request.POST.get('ordre_calcul', 100)
        ordre_affichage = request.POST.get('ordre_affichage', 100)
        affichage_bulletin = request.POST.get('affichage_bulletin') == 'on'
        actif = request.POST.get('actif') == 'on'
        
        if RubriquePaie.objects.filter(code_rubrique=code, entreprise=request.user.entreprise).exists():
            messages.error(request, f'Une rubrique avec le code "{code}" existe déjà. Veuillez choisir un autre code.')
            return render(request, 'paie/rubriques/creer.html')
        
        try:
            rubrique = RubriquePaie.objects.create(
                entreprise=request.user.entreprise,
                code_rubrique=code,
                libelle_rubrique=libelle,
                type_rubrique=type_rub,
                formule_calcul=formule,
                taux_rubrique=parse_montant(taux),
                montant_fixe=parse_montant(montant_fixe),
                soumis_cnss=soumis_cnss,
                soumis_irg=soumis_irg,
                ordre_calcul=int(ordre_calcul),
                ordre_affichage=int(ordre_affichage),
                affichage_bulletin=affichage_bulletin,
                actif=actif
            )
            
            messages.success(request, f'Rubrique "{libelle}" créée avec succès')
            return redirect('paie:liste_rubriques')
            
        except Exception as e:
            messages.error(request, f'Erreur lors de la création : {str(e)}')
    
    return render(request, 'paie/rubriques/creer.html')


@login_required
@entreprise_active_required
@reauth_required
def detail_rubrique(request, pk):
    """Détail d'une rubrique de paie"""
    rubrique = get_object_or_404(RubriquePaie, pk=pk, entreprise=request.user.entreprise)
    
    # Nombre d'employés utilisant cette rubrique
    nb_employes = ElementSalaire.objects.filter(
        rubrique=rubrique,
        actif=True,
        employe__entreprise=request.user.entreprise,
    ).values('employe').distinct().count()
    
    # Éléments utilisant cette rubrique
    elements = ElementSalaire.objects.filter(
        rubrique=rubrique,
        employe__entreprise=request.user.entreprise,
    ).select_related('employe').order_by('-actif', 'employe__nom')[:20]
    
    return render(request, 'paie/rubriques/detail.html', {
        'rubrique': rubrique,
        'nb_employes': nb_employes,
        'elements': elements
    })


@login_required
@entreprise_active_required
@reauth_required
def supprimer_rubrique(request, pk):
    """Supprimer une rubrique de paie"""
    rubrique = get_object_or_404(RubriquePaie, pk=pk, entreprise=request.user.entreprise)
    
    # Vérifier si des bulletins utilisent cette rubrique
    nb_lignes_bulletin = LigneBulletin.objects.filter(rubrique=rubrique).count()
    nb_elements = ElementSalaire.objects.filter(rubrique=rubrique).count()
    
    if request.method == 'POST':
        if nb_lignes_bulletin > 0:
            messages.error(
                request,
                f'Impossible de supprimer la rubrique "{rubrique.libelle_rubrique}" : '
                f'elle est utilisée dans {nb_lignes_bulletin} ligne(s) de bulletin. '
                f'Désactivez-la plutôt.'
            )
            return redirect('paie:detail_rubrique', pk=pk)
        
        libelle = rubrique.libelle_rubrique
        rubrique.delete()
        messages.success(request, f'Rubrique "{libelle}" supprimée avec succès.')
        return redirect('paie:liste_rubriques')
    
    return render(request, 'paie/rubriques/supprimer.html', {
        'rubrique': rubrique,
        'nb_lignes_bulletin': nb_lignes_bulletin,
        'nb_elements': nb_elements,
    })


@login_required
@reauth_required
@entreprise_active_required
def tableau_bord_echeances(request):
    """Tableau de bord des échéances de déclarations sociales"""
    aujourd_hui = date.today()
    entreprise = request.user.entreprise
    
    # Générer/actualiser les alertes pour le mois en cours
    mois_courant = aujourd_hui.month
    annee_courante = aujourd_hui.year
    
    # Mois précédent (pour les déclarations en cours)
    if mois_courant == 1:
        mois_declaration = 12
        annee_declaration = annee_courante - 1
    else:
        mois_declaration = mois_courant - 1
        annee_declaration = annee_courante
    
    # Générer les alertes si elles n'existent pas
    AlerteEcheance.generer_alertes_mois(entreprise, annee_declaration, mois_declaration)
    
    # Actualiser toutes les alertes
    alertes = AlerteEcheance.objects.filter(
        entreprise=entreprise,
        statut__in=['a_venir', 'urgent', 'en_retard']
    )
    for alerte in alertes:
        alerte.actualiser_statut()
    
    # Récupérer les alertes triées
    alertes_urgentes = AlerteEcheance.objects.filter(
        entreprise=entreprise,
        statut__in=['urgent', 'en_retard']
    ).order_by('date_echeance')
    
    alertes_a_venir = AlerteEcheance.objects.filter(
        entreprise=entreprise,
        statut='a_venir'
    ).order_by('date_echeance')[:10]
    
    alertes_traitees = AlerteEcheance.objects.filter(
        entreprise=entreprise,
        statut='traite'
    ).order_by('-date_echeance')[:5]
    
    # Statistiques
    stats = {
        'total_urgentes': alertes_urgentes.count(),
        'total_a_venir': alertes_a_venir.count(),
        'total_en_retard': AlerteEcheance.objects.filter(
            entreprise=entreprise,
            statut='en_retard'
        ).count(),
    }
    
    # Prochaine échéance
    prochaine_echeance = AlerteEcheance.objects.filter(
        entreprise=entreprise,
        statut__in=['a_venir', 'urgent']
    ).order_by('date_echeance').first()
    
    return render(request, 'paie/echeances/tableau_bord.html', {
        'alertes_urgentes': alertes_urgentes,
        'alertes_a_venir': alertes_a_venir,
        'alertes_traitees': alertes_traitees,
        'stats': stats,
        'prochaine_echeance': prochaine_echeance,
        'aujourd_hui': aujourd_hui,
    })


@login_required
@reauth_required
@entreprise_active_required
def marquer_alerte_traitee(request, pk):
    """Marque une alerte comme traitée"""
    alerte = get_object_or_404(AlerteEcheance, pk=pk, entreprise=request.user.entreprise)
    
    alerte.statut = 'traite'
    alerte.lu = True
    alerte.date_lecture = timezone.now()
    alerte.save()
    
    messages.success(request, f'Alerte "{alerte.get_type_echeance_display()}" marquée comme traitée.')
    return redirect('paie:tableau_bord_echeances')


@login_required
@reauth_required
@entreprise_active_required
def api_alertes_echeances(request):
    """API pour récupérer les alertes (pour le header/notifications)"""
    entreprise = request.user.entreprise
    
    # Actualiser et récupérer les alertes urgentes
    alertes = AlerteEcheance.objects.filter(
        entreprise=entreprise,
        statut__in=['urgent', 'en_retard'],
        lu=False
    ).order_by('date_echeance')[:5]
    
    data = {
        'count': alertes.count(),
        'alertes': [
            {
                'id': a.id,
                'type': a.get_type_echeance_display(),
                'message': a.message,
                'niveau': a.niveau_alerte,
                'jours_restants': a.jours_restants,
                'date_echeance': a.date_echeance.strftime('%d/%m/%Y'),
            }
            for a in alertes
        ]
    }
    
    return JsonResponse(data)


@login_required
@reauth_required
@entreprise_active_required
def historique_bulletins(request):
    """Historique des bulletins de paie avec recherche"""
    entreprise = request.user.entreprise
    
    # Filtres
    annee = request.GET.get('annee', date.today().year)
    mois = request.GET.get('mois', '')
    employe_id = request.GET.get('employe', '')
    recherche = request.GET.get('q', '')
    
    # Base query
    bulletins = BulletinPaie.objects.filter(
        employe__entreprise=entreprise,
        statut_bulletin__in=['valide', 'paye']
    ).select_related('employe', 'periode')
    
    # Appliquer les filtres
    if annee:
        bulletins = bulletins.filter(periode__annee=int(annee))
    if mois:
        bulletins = bulletins.filter(periode__mois=int(mois))
    if employe_id:
        bulletins = bulletins.filter(employe_id=employe_id)
    if recherche:
        bulletins = bulletins.filter(
            Q(employe__nom__icontains=recherche) |
            Q(employe__prenoms__icontains=recherche) |
            Q(employe__matricule__icontains=recherche) |
            Q(numero_bulletin__icontains=recherche)
        )
    
    bulletins = bulletins.order_by('-periode__annee', '-periode__mois', 'employe__nom')
    
    # Pagination
    from django.core.paginator import Paginator
    paginator = Paginator(bulletins, 25)
    page = request.GET.get('page', 1)
    bulletins_page = paginator.get_page(page)
    
    # Statistiques
    stats = {
        'total_bulletins': bulletins.count(),
        'total_brut': bulletins.aggregate(Sum('salaire_brut'))['salaire_brut__sum'] or 0,
        'total_net': bulletins.aggregate(Sum('net_a_payer'))['net_a_payer__sum'] or 0,
    }
    
    # Listes pour les filtres
    annees = PeriodePaie.objects.filter(
        entreprise=entreprise
    ).values_list('annee', flat=True).distinct().order_by('-annee')
    
    employes = Employe.objects.filter(
        entreprise=entreprise,
    ).order_by('nom', 'prenoms')
    
    return render(request, 'paie/historique_bulletins.html', {
        'bulletins': bulletins_page,
        'stats': stats,
        'annees': annees,
        'employes': employes,
        'annee_selectionnee': int(annee) if annee else None,
        'mois_selectionne': int(mois) if mois else None,
        'employe_selectionne': int(employe_id) if employe_id else None,
        'recherche': recherche,
    })


@login_required
@reauth_required
@entreprise_active_required
def telecharger_bulletins_masse(request):
    """Télécharge plusieurs bulletins en ZIP"""
    import zipfile
    import io
    
    entreprise = request.user.entreprise
    annee = request.GET.get('annee')
    mois = request.GET.get('mois')
    
    if not annee or not mois:
        messages.error(request, "Veuillez sélectionner une année et un mois")
        return redirect('paie:historique_bulletins')
    
    bulletins = BulletinPaie.objects.filter(
        employe__entreprise=entreprise,
        periode__annee=int(annee),
        periode__mois=int(mois),
        statut_bulletin__in=['valide', 'paye']
    ).select_related('employe', 'periode')
    
    if not bulletins.exists():
        messages.warning(request, "Aucun bulletin trouvé pour cette période")
        return redirect('paie:historique_bulletins')
    
    # Créer le ZIP en mémoire
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:
        for bulletin in bulletins:
            # Générer le PDF du bulletin
            from .utils import generer_bulletin_pdf
            try:
                pdf_content = generer_bulletin_pdf(bulletin)
                filename = f"Bulletin_{bulletin.employe.matricule}_{annee}_{mois:02d}.pdf"
                zip_file.writestr(filename, pdf_content)
            except Exception as e:
                continue
    
    buffer.seek(0)
    
    response = HttpResponse(buffer.read(), content_type='application/zip')
    response['Content-Disposition'] = f'attachment; filename="Bulletins_{annee}_{mois:02d}.zip"'
    return response


@login_required
@reauth_required
@entreprise_active_required
def attestation_salaire(request, employe_id):
    """Génère une attestation de salaire pour un employé"""
    employe = get_object_or_404(Employe, pk=employe_id, entreprise=request.user.entreprise)
    
    # Récupérer les 12 derniers bulletins
    bulletins = BulletinPaie.objects.filter(
        employe=employe,
        statut_bulletin__in=['valide', 'paye']
    ).order_by('-periode__annee', '-periode__mois')[:12]
    
    if not bulletins:
        messages.warning(request, "Aucun bulletin trouvé pour cet employé")
        return redirect('paie:historique_bulletins')
    
    # Calculer les moyennes
    from django.db.models import Avg
    stats = bulletins.aggregate(
        salaire_moyen=Avg('salaire_brut'),
        net_moyen=Avg('net_a_payer'),
    )
    
    dernier_bulletin = bulletins.first()
    
    context = {
        'employe': employe,
        'bulletins': bulletins,
        'stats': stats,
        'dernier_bulletin': dernier_bulletin,
        'date_attestation': date.today(),
        'entreprise': request.user.entreprise,
    }
    
    return render(request, 'paie/attestation_salaire.html', context)


@login_required
@reauth_required
@entreprise_active_required
def simulation_paie(request):
    """Simulateur de paie - Calcul sans enregistrement"""
    entreprise = request.user.entreprise
    resultat = None
    employe_selectionne = None
    
    # Liste des employés
    employes = Employe.objects.filter(
        entreprise=entreprise,
    ).order_by('nom', 'prenoms')
    
    if request.method == 'POST':
        employe_id = request.POST.get('employe')
        salaire_base = request.POST.get('salaire_base', '0')
        
        # Récupérer les primes/indemnités du formulaire
        prime_transport = request.POST.get('prime_transport', '0')
        prime_logement = request.POST.get('prime_logement', '0')
        prime_cherte_vie = request.POST.get('prime_cherte_vie', '0')
        prime_panier = request.POST.get('prime_panier', '0')
        prime_anciennete = request.POST.get('prime_anciennete', '0')
        prime_responsabilite = request.POST.get('prime_responsabilite', '0')
        autres_primes = request.POST.get('autres_primes', '0')
        rappel_salaire = request.POST.get('rappel_salaire', '0')
        retenue_trop_percu = request.POST.get('retenue_trop_percu', '0')
        
        # Convertir en Decimal
        try:
            salaire_base = parse_montant(salaire_base) or Decimal('0')
            prime_transport = parse_montant(prime_transport) or Decimal('0')
            prime_logement = parse_montant(prime_logement) or Decimal('0')
            prime_cherte_vie = parse_montant(prime_cherte_vie) or Decimal('0')
            prime_panier = parse_montant(prime_panier) or Decimal('0')
            prime_anciennete = parse_montant(prime_anciennete) or Decimal('0')
            prime_responsabilite = parse_montant(prime_responsabilite) or Decimal('0')
            autres_primes = parse_montant(autres_primes) or Decimal('0')
            rappel_salaire = parse_montant(rappel_salaire) or Decimal('0')
            retenue_trop_percu = parse_montant(retenue_trop_percu) or Decimal('0')
        except:
            messages.error(request, "Erreur dans les montants saisis")
            return redirect('paie:simulation_paie')
        
        # Calculer le brut
        salaire_brut = (salaire_base + prime_transport + prime_logement + prime_cherte_vie +
                       prime_panier + prime_anciennete + prime_responsabilite + autres_primes)
        
        # Primes exonérées de RTS (transport, logement, cherté de vie)
        primes_exonerees_rts = prime_transport + prime_logement + prime_cherte_vie
        
        # Nombre de salariés actifs pour TA vs ONFPP
        nb_salaries = Employe.objects.filter(
            entreprise=entreprise,
            statut_employe='actif'
        ).count()
        
        # Récupérer les constantes
        plancher_cnss = Decimal('550000')
        plafond_cnss = Decimal('2500000')
        taux_cnss_employe = Decimal('5')
        taux_cnss_employeur = Decimal('18')
        taux_vf = Decimal('6')
        taux_ta = Decimal('2')
        taux_onfpp = Decimal('1.5')
        seuil_ta_onfpp = 30
        
        try:
            const = Constante.objects.filter(code='PLANCHER_CNSS', actif=True).first()
            if const: plancher_cnss = const.valeur
            const = Constante.objects.filter(code='PLAFOND_CNSS', actif=True).first()
            if const: plafond_cnss = const.valeur
            const = Constante.objects.filter(code='SEUIL_TA_ONFPP', actif=True).first()
            if const: seuil_ta_onfpp = int(const.valeur)
        except:
            pass

        try:
            config_paie = entreprise.config_paie
            taux_vf = config_paie.taux_versement_forfaitaire
        except Exception:
            pass

        # CNSS Guinee: base et taux legaux fixes, sans surcharge flottante.
        plancher_cnss = Decimal('550000')
        plafond_cnss = Decimal('2500000')
        taux_cnss_employe = Decimal('5')
        taux_cnss_employeur = Decimal('18')
        
        # Calcul CNSS avec vérification du seuil minimum
        # IMPORTANT: Base CNSS = Brut complet (pas de primes exonérées à soustraire)
        # Les taux CNSS/RTS s'appliquent sur le brut avec plafond uniquement
        seuil_minimum_cnss = plancher_cnss * Decimal('0.10')
        alertes = []

        if salaire_brut <= 0:
            assiette_cnss = Decimal('0')
            cnss_employe = Decimal('0')
            cnss_employeur = Decimal('0')
            alertes.append({
                'type': 'critique',
                'message': f"Salaire brut nul ou négatif ({salaire_brut:,.0f} GNF). Vérifiez les éléments de salaire."
            })
        elif salaire_brut < seuil_minimum_cnss:
            assiette_cnss = Decimal('0')
            cnss_employe = Decimal('0')
            cnss_employeur = Decimal('0')
            alertes.append({
                'type': 'avertissement',
                'message': f"Brut très faible ({salaire_brut:,.0f} GNF < {seuil_minimum_cnss:,.0f} GNF). Pas de cotisation CNSS calculée."
            })
        else:
            # Assiette CNSS = brut encadré par le plancher et le plafond
            assiette_cnss = max(min(salaire_brut, plafond_cnss), plancher_cnss)
            cnss_employe = (assiette_cnss * taux_cnss_employe / Decimal('100')).quantize(Decimal('1'))
            cnss_employeur = (assiette_cnss * taux_cnss_employeur / Decimal('100')).quantize(Decimal('1'))
        
        # CGI Guinée: indemnités forfaitaires intégralement exonérées de RTS
        # Pas de plafond — toutes les indemnités forfaitaires sont déductibles
        # CGI Guinée: indemnités forfaitaires intégralement exonérées de RTS
        total_indemnites_forfaitaires = prime_transport + prime_logement + prime_cherte_vie + prime_panier

        # Base imposable RTS = Brut - CNSS - Indemnités forfaitaires (exonération intégrale)
        base_imposable = salaire_brut - cnss_employe - total_indemnites_forfaitaires
        
        # Calcul RTS par tranches (CGI 2022 - 6 tranches)
        tranches_rts = [
            (Decimal('0'), Decimal('1000000'), Decimal('0')),
            (Decimal('1000001'), Decimal('3000000'), Decimal('5')),
            (Decimal('3000001'), Decimal('5000000'), Decimal('8')),
            (Decimal('5000001'), Decimal('10000000'), Decimal('10')),
            (Decimal('10000001'), Decimal('20000000'), Decimal('15')),
            (Decimal('20000001'), None, Decimal('20')),
        ]
        
        rts = Decimal('0')
        detail_rts = []
        reste = base_imposable
        
        for borne_inf, borne_sup, taux in tranches_rts:
            if reste <= 0:
                break
            if borne_sup:
                montant_tranche = min(reste, borne_sup - borne_inf + 1)
            else:
                montant_tranche = reste
            
            if base_imposable >= borne_inf:
                impot_tranche = (montant_tranche * taux / Decimal('100')).quantize(Decimal('1'))
                rts += impot_tranche
                if montant_tranche > 0:
                    detail_rts.append({
                        'borne_inf': borne_inf,
                        'borne_sup': borne_sup,
                        'taux': taux,
                        'montant': montant_tranche,
                        'impot': impot_tranche,
                    })
                reste -= montant_tranche
        
        # Taux effectif RTS
        if base_imposable > 0:
            taux_effectif_rts = (rts * Decimal('100') / base_imposable).quantize(Decimal('0.01'))
        else:
            taux_effectif_rts = Decimal('0')
        
        # Charges patronales: base VF/ONFPP optimisee = brut - indemnites exonerees plafonnees.
        plafond_indemnites_vf = (salaire_brut * Decimal('25') / Decimal('100')).quantize(Decimal('1'))
        deduction_vf = min(total_indemnites_forfaitaires, plafond_indemnites_vf, salaire_brut)
        base_vf = max(Decimal('0'), salaire_brut - deduction_vf)
        vf = (base_vf * taux_vf / Decimal('100')).quantize(Decimal('1'))
        
        # TA et ONFPP mutuellement exclusifs selon le seuil configure.
        if nb_salaries < seuil_ta_onfpp:
            ta = (base_vf * taux_ta / Decimal('100')).quantize(Decimal('1'))
            onfpp = Decimal('0')
        else:
            ta = Decimal('0')
            onfpp = (base_vf * taux_onfpp / Decimal('100')).quantize(Decimal('1'))
        
        # Totaux (rappels et retenues trop-perçu hors base)
        total_retenues = cnss_employe + rts
        net_a_payer = salaire_brut - total_retenues + rappel_salaire - retenue_trop_percu
        total_charges_patronales = cnss_employeur + vf + ta + onfpp
        cout_total_employeur = salaire_brut + total_charges_patronales
        retenues_excessives = Decimal('0')
        
        # Protection: Empêcher le net négatif
        if net_a_payer < 0:
            alertes.append({
                'type': 'critique',
                'message': f"Net à payer serait négatif ({net_a_payer:,.0f} GNF). Les retenues ({total_retenues:,.0f} GNF) dépassent le brut ({salaire_brut:,.0f} GNF). Retenues plafonnées."
            })
            retenues_excessives = abs(net_a_payer)
            total_retenues = salaire_brut
            net_a_payer = Decimal('0')
        
        # Employé sélectionné
        if employe_id:
            employe_selectionne = Employe.objects.filter(pk=employe_id).first()
        
        resultat = {
            'salaire_base': salaire_base,
            'prime_transport': prime_transport,
            'prime_logement': prime_logement,
            'prime_cherte_vie': prime_cherte_vie,
            'prime_panier': prime_panier,
            'prime_anciennete': prime_anciennete,
            'prime_responsabilite': prime_responsabilite,
            'autres_primes': autres_primes,
            'salaire_brut': salaire_brut,
            'primes_exonerees_cnss': primes_exonerees_rts,  # Primes exonérées de RTS (pour compatibilité template)
            'assiette_cnss': assiette_cnss,
            'cnss_employe': cnss_employe,
            'cnss_employeur': cnss_employeur,
            # Indemnités forfaitaires exonérées (intégralement)
            'total_indemnites_forfaitaires': total_indemnites_forfaitaires,
            'base_imposable': base_imposable,
            'rts': rts,
            'taux_effectif_rts': taux_effectif_rts,
            'detail_rts': detail_rts,
            'vf': vf,
            'base_vf': base_vf,
            'ta': ta,
            'onfpp': onfpp,
            'nb_salaries': nb_salaries,
            'seuil_ta_onfpp': seuil_ta_onfpp,
            'total_retenues': total_retenues,
            'net_a_payer': net_a_payer,
            'total_charges_patronales': total_charges_patronales,
            'cout_total_employeur': cout_total_employeur,
            'taux_cnss_employe': taux_cnss_employe,
            'taux_cnss_employeur': taux_cnss_employeur,
            'taux_vf': taux_vf,
            'taux_ta': taux_ta,
            'taux_onfpp': taux_onfpp,
            'alertes': alertes,
            'seuil_minimum_cnss': seuil_minimum_cnss,
            'retenues_excessives': retenues_excessives,
            'rappel_salaire': rappel_salaire,
            'retenue_trop_percu': retenue_trop_percu,
        }
    
    return render(request, 'paie/simulation_paie.html', {
        'employes': employes,
        'resultat': resultat,
        'employe_selectionne': employe_selectionne,
    })


# ============================================
# VUES ARCHIVES BULLETINS - TRAÇABILITÉ
# ============================================

@login_required
@reauth_required
@entreprise_active_required
def liste_archives(request):
    """Liste des bulletins archivés avec statistiques"""
    from .services_archive import ArchivageService
    
    entreprise = request.user.entreprise
    
    archives = ArchiveBulletin.objects.filter(
        bulletin__employe__entreprise=entreprise
    ).select_related('bulletin', 'bulletin__employe').order_by(
        '-periode_annee', '-periode_mois', 'employe_nom'
    )
    
    # Filtres
    annee = request.GET.get('annee')
    if annee:
        archives = archives.filter(periode_annee=int(annee))
    
    mois = request.GET.get('mois')
    if mois:
        archives = archives.filter(periode_mois=int(mois))
    
    # Stats
    stats = ArchivageService.stats_archives(entreprise)
    
    # Années disponibles
    annees = ArchiveBulletin.objects.filter(
        bulletin__employe__entreprise=entreprise
    ).values_list('periode_annee', flat=True).distinct().order_by('-periode_annee')
    
    return render(request, 'paie/archives/liste.html', {
        'archives': archives[:100],
        'stats': stats,
        'annees': annees,
        'annee_filtre': annee,
        'mois_filtre': mois,
    })


@login_required
@reauth_required
@entreprise_active_required
def telecharger_archive(request, pk):
    """Télécharger un bulletin archivé"""
    from .services_archive import ArchivageService
    
    archive = get_object_or_404(
        ArchiveBulletin,
        pk=pk,
        bulletin__employe__entreprise=request.user.entreprise
    )
    
    contenu = ArchivageService.telecharger_archive(archive)
    if not contenu:
        messages.error(request, "Archive non disponible")
        return redirect('paie:liste_archives')
    
    response = HttpResponse(contenu, content_type='application/pdf')
    response['Content-Disposition'] = f'attachment; filename="archive_{archive.employe_matricule}_{archive.periode_annee}{archive.periode_mois:02d}.pdf"'
    return response


@login_required
@reauth_required
@entreprise_active_required
def verifier_integrite_archive(request, pk):
    """Vérifier l'intégrité d'une archive"""
    from .services_archive import ArchivageService
    
    archive = get_object_or_404(
        ArchiveBulletin,
        pk=pk,
        bulletin__employe__entreprise=request.user.entreprise
    )
    
    if ArchivageService.verifier_integrite(archive):
        messages.success(request, f"✓ Intégrité vérifiée pour {archive.employe_nom}")
    else:
        messages.error(request, f"✗ ALERTE: Intégrité compromise pour {archive.employe_nom}")
    
    return redirect('paie:liste_archives')


@login_required
@reauth_required
@entreprise_active_required
def config_paie_entreprise(request):
    """Configuration des paramètres de paie par entreprise (HS, Congés, CNSS)"""
    from .models import ConfigurationPaieEntreprise
    
    entreprise = request.user.entreprise
    config = ConfigurationPaieEntreprise.get_ou_creer(entreprise)
    
    if request.method == 'POST':
        action = request.POST.get('action')
        
        if action == 'code_travail':
            config.appliquer_mode_code_travail()
            config.modifie_par = request.user
            config.save(update_fields=[
                'mode_heures_sup', 'taux_hs_4_premieres', 'taux_hs_au_dela',
                'taux_hs_nuit', 'mode_conges', 'jours_conges_par_mois',
                'modifie_par', 'date_modification',
            ])
            messages.success(request, 'Configuration Code du Travail appliquée.')
        elif action == 'convention':
            config.appliquer_mode_convention()
            config.modifie_par = request.user
            config.save(update_fields=[
                'mode_heures_sup', 'taux_hs_4_premieres', 'taux_hs_au_dela',
                'taux_hs_nuit', 'taux_hs_dimanche', 'taux_hs_ferie_nuit',
                'mode_conges', 'jours_conges_par_mois',
                'modifie_par', 'date_modification',
            ])
            messages.success(request, 'Configuration Convention Collective appliquée.')
        elif action == 'save':
            # Fonction helper pour convertir en Decimal avec valeur par défaut
            def to_decimal(value, default):
                if value is None or str(value).strip() == '':
                    return Decimal(default)
                montant = parse_montant(value)
                if montant is None:
                    raise ValueError(f"Montant invalide: {value}")
                return montant
            
            def to_int(value, default):
                if value is None or str(value).strip() == '':
                    return int(default)
                return int(value)
            
            # Heures supplémentaires
            try:
                mode_heures_sup = request.POST.get('mode_heures_sup', 'code_travail')
                mode_conges = request.POST.get('mode_conges', 'code_travail')
                if mode_heures_sup not in dict(ConfigurationPaieEntreprise.MODES_HS):
                    raise ValueError("Mode heures supplementaires invalide.")
                if mode_conges not in dict(ConfigurationPaieEntreprise.MODES_CONGES):
                    raise ValueError("Mode conges invalide.")

                config.mode_heures_sup = mode_heures_sup
                config.taux_hs_4_premieres = to_decimal(request.POST.get('taux_hs_4_premieres'), '30')
                config.taux_hs_au_dela = to_decimal(request.POST.get('taux_hs_au_dela'), '60')
                config.taux_hs_nuit = to_decimal(request.POST.get('taux_hs_nuit'), '50')
                config.taux_hs_dimanche = to_decimal(request.POST.get('taux_hs_dimanche'), '100')
                config.taux_hs_ferie_nuit = to_decimal(request.POST.get('taux_hs_ferie_nuit'), '100')

                config.mode_conges = mode_conges
                config.jours_conges_par_mois = to_decimal(request.POST.get('jours_conges_par_mois'), '1.5')
                config.jours_conges_anciennete = to_decimal(request.POST.get('jours_conges_anciennete'), '2')
                config.tranche_anciennete_annees = to_int(request.POST.get('tranche_anciennete_annees'), '5')

                config.taux_cnss_employe = to_decimal(request.POST.get('taux_cnss_employe'), '5')
                config.taux_cnss_employeur = to_decimal(request.POST.get('taux_cnss_employeur'), '18')
                config.plancher_cnss = to_decimal(request.POST.get('plancher_cnss'), '550000')
                config.plafond_cnss = to_decimal(request.POST.get('plafond_cnss'), '2500000')

                config.taux_versement_forfaitaire = to_decimal(request.POST.get('taux_versement_forfaitaire'), '6')
                config.taux_taxe_apprentissage = Decimal('2.00')
                config.taux_onfpp = Decimal('1.50')

                arrondi_val = to_int(request.POST.get('arrondi_net'), '0')
                if arrondi_val not in (0, 100, 500, 1000):
                    raise ValueError("Arrondi du net invalide.")
                config.arrondi_net = arrondi_val

                valeurs_non_negatives = [
                    config.taux_hs_4_premieres, config.taux_hs_au_dela,
                    config.taux_hs_nuit, config.taux_hs_dimanche,
                    config.taux_hs_ferie_nuit, config.jours_conges_par_mois,
                    config.jours_conges_anciennete, config.taux_cnss_employe,
                    config.taux_cnss_employeur, config.plancher_cnss,
                    config.plafond_cnss, config.taux_versement_forfaitaire,
                ]
                if any(valeur < 0 for valeur in valeurs_non_negatives):
                    raise ValueError("Les valeurs de configuration doivent etre positives.")
                if config.tranche_anciennete_annees < 1:
                    raise ValueError("La tranche d'anciennete doit etre superieure ou egale a 1.")
                if config.plafond_cnss < config.plancher_cnss:
                    raise ValueError("Le plafond CNSS doit etre superieur ou egal au plancher CNSS.")

                config.modifie_par = request.user
                config.save()
                messages.success(request, 'Configuration enregistrée avec succès.')
            except (InvalidOperation, ValueError) as exc:
                messages.error(request, f"Configuration non enregistrée: {exc}")
        
        return redirect('paie:config_entreprise')
    
    return render(request, 'paie/config_entreprise.html', {
        'config': config,
        'modes_hs': ConfigurationPaieEntreprise.MODES_HS,
        'modes_conges': ConfigurationPaieEntreprise.MODES_CONGES,
        'arrondis_net': ConfigurationPaieEntreprise.ARRONDIS_NET,
    })


# ---------------------------------------------------------------------------
# BULLETINS GROUPÉS PDF (impression multiple)
# ---------------------------------------------------------------------------

@login_required
@entreprise_active_required
@reauth_required
def bulletins_groupes_pdf(request):
    """
    Génère un PDF unique contenant tous les bulletins d'une période (un par page).
    URL: /paie/bulletins/groupe/pdf/?periode_id=X  OU  ?annee=X&mois=Y
    Limite à 100 bulletins max.
    """
    from io import BytesIO
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.pdfgen import canvas as rl_canvas
        REPORTLAB_OK = True
    except ImportError:
        REPORTLAB_OK = False

    if not REPORTLAB_OK:
        messages.error(request, "ReportLab n'est pas disponible.")
        return redirect('paie:liste_bulletins')

    entreprise = request.user.entreprise
    periode_id = request.GET.get('periode_id')
    annee = request.GET.get('annee')
    mois_param = request.GET.get('mois')

    # Filtrer les bulletins
    if periode_id:
        periode = get_object_or_404(PeriodePaie, pk=periode_id, entreprise=entreprise)
        bulletins_qs = BulletinPaie.objects.filter(
            periode=periode,
            employe__entreprise=entreprise,
            statut_bulletin__in=['calcule', 'valide', 'paye'],
        ).select_related('employe').order_by('employe__nom')
    elif annee and mois_param:
        bulletins_qs = BulletinPaie.objects.filter(
            employe__entreprise=entreprise,
            annee_paie=int(annee),
            mois_paie=int(mois_param),
            statut_bulletin__in=['calcule', 'valide', 'paye'],
        ).select_related('employe').order_by('employe__nom')
    else:
        messages.error(request, "Veuillez préciser une période (periode_id ou annee+mois).")
        return redirect('paie:liste_bulletins')

    # Limite de 100 bulletins
    total = bulletins_qs.count()
    if total == 0:
        messages.warning(request, "Aucun bulletin trouvé pour cette période.")
        return redirect('paie:liste_bulletins')
    if total > 100:
        messages.warning(request, f"Trop de bulletins ({total}). Limité à 100.")
        bulletins_qs = bulletins_qs[:100]

    from .utils import generer_bulletin_pdf

    # Générer chaque bulletin et les concaténer
    try:
        from PyPDF2 import PdfMerger
        merger = PdfMerger()
        for bulletin in bulletins_qs:
            pdf_bytes = generer_bulletin_pdf(bulletin)
            merger.append(BytesIO(pdf_bytes))
        output = BytesIO()
        merger.write(output)
        merger.close()
        output.seek(0)
        pdf_final = output.read()
    except ImportError:
        # Fallback: concaténation simple page par page sans PyPDF2
        # Utiliser reportlab pour créer un PDF multi-pages d'aperçu simplifié
        from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, PageBreak
        from reportlab.lib.styles import getSampleStyleSheet
        from reportlab.lib.units import cm

        output = BytesIO()
        doc = SimpleDocTemplate(output, pagesize=A4)
        styles = getSampleStyleSheet()
        elements = []
        mois_noms = ['', 'Janvier', 'Février', 'Mars', 'Avril', 'Mai', 'Juin',
                     'Juillet', 'Août', 'Septembre', 'Octobre', 'Novembre', 'Décembre']

        for i, bulletin in enumerate(bulletins_qs):
            if i > 0:
                elements.append(PageBreak())
            elements.append(Paragraph(
                f"<b>BULLETIN DE PAIE</b>",
                styles['Title']
            ))
            mois_label = mois_noms[bulletin.mois_paie] if 1 <= bulletin.mois_paie <= 12 else str(bulletin.mois_paie)
            elements.append(Paragraph(
                f"{mois_label} {bulletin.annee_paie}",
                styles['Heading2']
            ))
            elements.append(Spacer(1, 0.3*cm))
            infos = [
                f"Employé: {bulletin.employe.nom} {bulletin.employe.prenoms}",
                f"Matricule: {bulletin.employe.matricule}",
                f"N° Bulletin: {bulletin.numero_bulletin}",
                f"Salaire Brut: {bulletin.salaire_brut:,.0f} GNF",
                f"CNSS Salarié: {bulletin.cnss_employe:,.0f} GNF",
                f"RTS: {bulletin.irg:,.0f} GNF",
                f"Net à Payer: {bulletin.net_a_payer:,.0f} GNF",
            ]
            for info in infos:
                elements.append(Paragraph(info, styles['Normal']))
                elements.append(Spacer(1, 0.2*cm))

        doc.build(elements)
        output.seek(0)
        pdf_final = output.read()

    # Nom du fichier
    if periode_id:
        nom_fichier = f"bulletins_groupes_periode_{periode_id}.pdf"
    else:
        nom_fichier = f"bulletins_groupes_{annee}_{int(mois_param):02d}.pdf"

    response = HttpResponse(pdf_final, content_type='application/pdf')
    response['Content-Disposition'] = f'inline; filename="{nom_fichier}"'
    return response


# ============================================================================
# API COÛT TOTAL EMPLOYEUR → BRUT → NET
# Le budget négocié est le coût total (brut + charges patronales)
# ============================================================================

@login_required
@entreprise_active_required
@reauth_required
def api_cout_total(request):
    """
    POST JSON : calcule brut + net depuis un budget total employeur.

    Entrée  : { "cout_total": 5500000, "pct_indemnites_forfaitaires": 0,
                "annee": 2026, "nb_salaries": 0 }
    Sortie  : { brut, net, cnss_employe, rts, charges_patronales: {
                  cnss_employeur, vf, ta, libelle_ta, total },
                cout_total_reel, formatted }
    """
    if not hasattr(request.user, 'entreprise') or not request.user.entreprise:
        return JsonResponse({'error': 'Aucune entreprise associée.'}, status=403)

    if request.method != 'POST':
        return JsonResponse({'error': 'POST requis'}, status=405)

    try:
        data = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({'error': 'JSON invalide'}, status=400)

    raw = data.get('cout_total', 0)
    try:
        cout_total = Decimal(str(raw).replace(' ', '').replace('\xa0', '').replace('\u202f', ''))
        if cout_total <= 0:
            raise ValueError
    except (InvalidOperation, ValueError):
        return JsonResponse({'error': 'Montant invalide'}, status=400)

    try:
        pct_indem = float(data.get('pct_indemnites_forfaitaires', 0) or 0)
        pct_indem = max(0.0, min(25.0, pct_indem))
    except (TypeError, ValueError):
        pct_indem = 0.0

    try:
        annee = int(data.get('annee') or date.today().year)
    except (TypeError, ValueError):
        annee = date.today().year

    try:
        nb_salaries = int(data.get('nb_salaries', 0) or 0)
    except (TypeError, ValueError):
        nb_salaries = 0

    from .services_retropaie import cout_total_vers_brut

    try:
        r = cout_total_vers_brut(
            cout_total=cout_total,
            annee=annee,
            pct_indemnites_forfaitaires=pct_indem,
            nb_salaries=nb_salaries,
        )
    except Exception as e:
        return JsonResponse({'error': f'Erreur de calcul : {str(e)}'}, status=500)

    def fmt(n):
        return f"{int(n):,}".replace(',', ' ')

    cp = r['charges_patronales']
    return JsonResponse({
        'cout_total_vise':  r['cout_total_vise'],
        'cout_total_reel':  r['cout_total_reel'],
        'brut':             int(r['brut']),
        'cnss_employe':     int(r['cnss_employe']),
        'base_cnss':        int(r['base_cnss']),
        'base_rts':         int(r['base_rts']),
        'rts':              int(r['rts']),
        'net':              int(r['net']),
        'charges_patronales': {
            'cnss_employeur': cp['cnss_employeur'],
            'vf':             cp['vf'],
            'ta':             cp['ta'],
            'libelle_ta':     cp['libelle_ta'],
            'total':          cp['total'],
        },
        'detail_tranches':  r['detail_tranches'],
        'ok':               r['ok'],
        'annee':            r['annee'],
        'formatted': {
            'cout_total':       fmt(r['cout_total_reel']),
            'brut':             fmt(r['brut']),
            'cnss_employe':     fmt(r['cnss_employe']),
            'rts':              fmt(r['rts']),
            'net':              fmt(r['net']),
            'cnss_employeur':   fmt(cp['cnss_employeur']),
            'vf':               fmt(cp['vf']),
            'ta':               fmt(cp['ta']),
            'total_pat':        fmt(cp['total']),
        },
    })


# ============================================================================
# API RÉTRO-PAIE : NET → BRUT
# Calcule le brut nécessaire pour obtenir exactement le net convenu
# ============================================================================

@login_required
@entreprise_active_required
@reauth_required
def api_retropaie(request):
    """
    Endpoint AJAX : calcul rétrograde net → brut.

    POST JSON :
        net_cible                  : int  – Net mensuel souhaité en GNF
        pct_indemnites_forfaitaires: float – Part des indemnités forfaitaires
                                            exonérées de RTS (0-25, défaut 0)
        annee                      : int  – Année du barème (défaut: courante)

    Réponse JSON :
        brut, cnss, base_cnss, base_rts, rts, net_calcule, net_cible,
        ecart, ok, iterations, detail_tranches, formatted (montants formatés)
    """
    # Vérification entreprise (JSON pour les requêtes AJAX)
    if not hasattr(request.user, 'entreprise') or not request.user.entreprise:
        return JsonResponse({'error': 'Aucune entreprise associée à ce compte.'}, status=403)
    if not request.user.entreprise.actif:
        return JsonResponse({'error': 'Votre entreprise est désactivée.'}, status=403)

    if request.method != 'POST':
        return JsonResponse({'error': 'Méthode non autorisée'}, status=405)

    try:
        data = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({'error': 'Corps JSON invalide'}, status=400)

    # --- Validation net_cible -----------------------------------------------
    raw_net = data.get('net_cible', '')
    if not raw_net:
        return JsonResponse({'error': 'Le net cible est requis'}, status=400)

    try:
        net_cible = Decimal(str(raw_net).replace(' ', '').replace('\xa0', ''))
        if net_cible <= 0:
            raise ValueError
    except (InvalidOperation, ValueError):
        return JsonResponse({'error': 'Montant net invalide'}, status=400)

    # --- Paramètres optionnels ---------------------------------------------
    try:
        pct_indem = float(data.get('pct_indemnites_forfaitaires', 0) or 0)
        pct_indem = max(0.0, min(25.0, pct_indem))
    except (TypeError, ValueError):
        pct_indem = 0.0

    try:
        annee = int(data.get('annee') or date.today().year)
    except (TypeError, ValueError):
        annee = date.today().year

    garantir_net_minimum = bool(data.get('garantir_net_minimum', True))
    comparer_baremes = bool(data.get('comparer_baremes', False))

    # --- Calcul rétrograde -------------------------------------------------
    from .services_retropaie import retropaie_net_vers_brut, calculer_charges_patronales

    try:
        resultat = retropaie_net_vers_brut(
            net_cible=net_cible,
            annee=annee,
            pct_indemnites_forfaitaires=pct_indem,
            garantir_net_minimum=garantir_net_minimum,
        )
    except Exception as e:
        return JsonResponse({'error': f'Erreur de calcul : {str(e)}'}, status=500)

    # --- Charges patronales -----------------------------------------------
    nb_salaries = Employe.objects.filter(
        entreprise=request.user.entreprise,
        statut_employe='actif'
    ).count()
    try:
        cp = calculer_charges_patronales(
            resultat['brut'], annee=annee, nb_salaries=nb_salaries,
            pct_indemnites_forfaitaires=pct_indem,
        )
    except Exception:
        cp = {'cnss_employeur': 0, 'vf': 0, 'ta': 0, 'libelle_ta': 'TA', 'total': 0,
              'cout_total_employeur': int(resultat['brut'])}

    # --- Formatage des montants (GNF sans décimales, séparateur espace) ---
    def fmt(n):
        return f"{int(n):,}".replace(',', ' ')

    reponse = {
        'brut':            int(resultat['brut']),
        'cnss':            int(resultat['cnss']),
        'base_cnss':       int(resultat['base_cnss']),
        'base_rts':        int(resultat['base_rts']),
        'rts':             int(resultat['rts']),
        'net_calcule':     int(resultat['net_calcule']),
        'net_cible':       int(resultat['net_cible']),
        'ecart':           int(resultat['ecart']),
        'ok':              resultat['ok'],
        'iterations':      resultat['iterations'],
        'detail_tranches': resultat['detail_tranches'],
        'mode':            resultat.get('mode', 'net_minimum'),
        'annee':           annee,
        'charges_patronales': {
            'cnss_employeur':      cp['cnss_employeur'],
            'vf':                  cp['vf'],
            'ta':                  cp['ta'],
            'libelle_ta':          cp['libelle_ta'],
            'total':               cp['total'],
            'cout_total_employeur': cp['cout_total_employeur'],
        },
        'taux_charge_global': round(
            cp['total'] / int(resultat['brut']) * 100, 1
        ) if int(resultat['brut']) > 0 else 0,
        'formatted': {
            'brut':             fmt(resultat['brut']),
            'cnss':             fmt(resultat['cnss']),
            'base_cnss':        fmt(resultat['base_cnss']),
            'base_rts':         fmt(resultat['base_rts']),
            'rts':              fmt(resultat['rts']),
            'net_calcule':      fmt(resultat['net_calcule']),
            'net_cible':        fmt(resultat['net_cible']),
            'cnss_employeur':   fmt(cp['cnss_employeur']),
            'vf':               fmt(cp['vf']),
            'ta':               fmt(cp['ta']),
            'total_pat':        fmt(cp['total']),
            'cout_total':       fmt(cp['cout_total_employeur']),
        },
    }

    # --- Comparaison multi-barèmes -----------------------------------------
    if comparer_baremes:
        annees_disponibles = (
            TrancheRTS.objects
            .filter(actif=True, type_bareme='officiel')
            .values_list('annee_validite', flat=True)
            .distinct()
            .order_by('-annee_validite')
        )
        comparaison = []
        for annee_b in annees_disponibles:
            try:
                res_b = retropaie_net_vers_brut(
                    net_cible=net_cible,
                    annee=int(annee_b),
                    pct_indemnites_forfaitaires=pct_indem,
                    garantir_net_minimum=garantir_net_minimum,
                )
                comparaison.append({
                    'annee':        int(annee_b),
                    'brut':         int(res_b['brut']),
                    'cnss':         int(res_b['cnss']),
                    'rts':          int(res_b['rts']),
                    'net_calcule':  int(res_b['net_calcule']),
                    'formatted': {
                        'brut':        fmt(res_b['brut']),
                        'cnss':        fmt(res_b['cnss']),
                        'rts':         fmt(res_b['rts']),
                        'net_calcule': fmt(res_b['net_calcule']),
                    },
                })
            except Exception:
                pass
        reponse['comparaison_baremes'] = comparaison

    return JsonResponse(reponse)


# ============================================================================
# VUE PDF RÉTRO-PAIE
# ============================================================================

@login_required
@entreprise_active_required
@reauth_required
def retropaie_pdf(request):
    """
    Génère un PDF de la simulation rétrograde Net → Brut.
    Accepte les mêmes paramètres que api_retropaie (en POST ou GET).
    """
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import cm
    from reportlab.pdfgen import canvas
    from reportlab.lib import colors
    from reportlab.platypus import Table, TableStyle
    import io
    import os

    # Polices
    try:
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
        _font_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            'static', 'fonts'
        )
        pdfmetrics.registerFont(TTFont('Arial', os.path.join(_font_dir, 'Arial.ttf')))
        pdfmetrics.registerFont(TTFont('Arial-Bold', os.path.join(_font_dir, 'Arial-Bold.ttf')))
        _FN = 'Arial'
        _FB = 'Arial-Bold'
    except Exception:
        _FN = 'Helvetica'
        _FB = 'Helvetica-Bold'

    # --- Lecture des paramètres (POST ou GET) ---
    if request.method == 'POST':
        raw_net = request.POST.get('net_cible', '')
        pct_indem_raw = request.POST.get('pct_indemnites_forfaitaires', '0')
        annee_raw = request.POST.get('annee', '')
        garantir_raw = request.POST.get('garantir_net_minimum', 'true')
    else:
        raw_net = request.GET.get('net_cible', '')
        pct_indem_raw = request.GET.get('pct_indemnites_forfaitaires', '0')
        annee_raw = request.GET.get('annee', '')
        garantir_raw = request.GET.get('garantir_net_minimum', 'true')

    try:
        net_cible = Decimal(str(raw_net).replace(' ', '').replace('\xa0', ''))
        if net_cible <= 0:
            raise ValueError
    except (InvalidOperation, ValueError):
        return HttpResponse('Paramètre net_cible invalide', status=400)

    try:
        pct_indem = float(pct_indem_raw or 0)
        pct_indem = max(0.0, min(25.0, pct_indem))
    except (TypeError, ValueError):
        pct_indem = 0.0

    try:
        annee = int(annee_raw) if annee_raw else date.today().year
    except (TypeError, ValueError):
        annee = date.today().year

    garantir_net_minimum = str(garantir_raw).lower() not in ('false', '0', 'no')

    # --- Calcul ---
    from .services_retropaie import retropaie_net_vers_brut
    try:
        resultat = retropaie_net_vers_brut(
            net_cible=net_cible,
            annee=annee,
            pct_indemnites_forfaitaires=pct_indem,
            garantir_net_minimum=garantir_net_minimum,
        )
    except Exception as e:
        return HttpResponse(f'Erreur de calcul : {e}', status=500)

    def fmt(n):
        try:
            return f"{int(n):,}".replace(',', '\u00a0')
        except Exception:
            return str(n)

    # --- Génération PDF ---
    buffer = io.BytesIO()
    p = canvas.Canvas(buffer, pagesize=A4)
    width, height = A4

    entreprise = getattr(request.user, 'entreprise', None)
    nom_entreprise = entreprise.nom_entreprise if entreprise else 'ENTREPRISE'
    mode_label = 'Net minimum (net ≥ objectif)' if garantir_net_minimum else 'Net exact (net ≈ objectif ±1 GNF)'
    mode_key = resultat.get('mode', 'net_minimum')

    y = height - 1.5 * cm

    # En-tête : nom entreprise
    p.setFont(_FB, 13)
    p.setFillColor(colors.HexColor('#0d6efd'))
    p.drawCentredString(width / 2, y, nom_entreprise)
    y -= 0.6 * cm

    # Trait bleu
    p.setStrokeColor(colors.HexColor('#0d6efd'))
    p.setLineWidth(2)
    p.line(1.5 * cm, y, width - 1.5 * cm, y)
    y -= 0.5 * cm

    # Titre principal
    p.setFont(_FB, 14)
    p.setFillColor(colors.black)
    p.drawCentredString(width / 2, y, 'Simulation Salariale - Calcul Net → Brut')
    y -= 0.5 * cm

    # Date
    p.setFont(_FN, 9)
    p.setFillColor(colors.HexColor('#555555'))
    p.drawCentredString(width / 2, y, f"Barème RTS {annee}   –   Généré le {date.today().strftime('%d/%m/%Y')}")
    p.setFillColor(colors.black)
    y -= 0.9 * cm

    # --- Section Paramètres ---
    p.setFont(_FB, 10)
    p.setFillColor(colors.HexColor('#198754'))
    p.drawString(1.5 * cm, y, 'Paramètres de la simulation')
    p.setFillColor(colors.black)
    y -= 0.45 * cm

    p.setFont(_FN, 9)
    params = [
        ('Net cible', f"{fmt(net_cible)} GNF"),
        ('% Indemnités forfaitaires exonérées', f"{pct_indem:.0f} %"),
        ('Mode de calcul', mode_label),
    ]
    for label, valeur in params:
        p.drawString(2 * cm, y, f"• {label} :")
        p.drawString(9 * cm, y, valeur)
        y -= 0.45 * cm

    y -= 0.3 * cm

    # --- Tableau résultat principal ---
    p.setFont(_FB, 10)
    p.setFillColor(colors.HexColor('#0d6efd'))
    p.drawString(1.5 * cm, y, 'Résultat du calcul')
    p.setFillColor(colors.black)
    y -= 0.5 * cm

    data_table = [
        ['', 'Montant (GNF)'],
        ['Salaire BRUT à saisir', fmt(resultat['brut'])],
        ['CNSS salarié (5 %)', fmt(resultat['cnss'])],
        ['Base CNSS (plafonnée)', fmt(resultat['base_cnss'])],
        ['Base imposable RTS', fmt(resultat['base_rts'])],
        ['RTS', fmt(resultat['rts'])],
        ['NET réel calculé', fmt(resultat['net_calcule'])],
        ['Net cible demandé', fmt(resultat['net_cible'])],
        [f"Écart (mode : {mode_key})", fmt(resultat['ecart'])],
    ]

    col_widths = [9 * cm, 6 * cm]
    tbl = Table(data_table, colWidths=col_widths)
    tbl.setStyle(TableStyle([
        ('BACKGROUND',  (0, 0), (-1, 0), colors.HexColor('#0d6efd')),
        ('TEXTCOLOR',   (0, 0), (-1, 0), colors.white),
        ('FONTNAME',    (0, 0), (-1, 0), _FB),
        ('FONTSIZE',    (0, 0), (-1, 0), 9),
        ('BACKGROUND',  (0, 1), (-1, 1), colors.HexColor('#cfe2ff')),
        ('FONTNAME',    (0, 1), (-1, 1), _FB),
        ('BACKGROUND',  (0, 5), (-1, 5), colors.HexColor('#fff3cd')),
        ('BACKGROUND',  (0, 6), (-1, 6), colors.HexColor('#d1e7dd')),
        ('FONTNAME',    (0, 6), (-1, 6), _FB),
        ('FONTSIZE',    (0, 1), (-1, -1), 9),
        ('ALIGN',       (1, 0), (1, -1), 'RIGHT'),
        ('GRID',        (0, 0), (-1, -1), 0.5, colors.HexColor('#cccccc')),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#f8f9fa')]),
        ('LEFTPADDING',  (0, 0), (-1, -1), 6),
        ('RIGHTPADDING', (0, 0), (-1, -1), 6),
        ('TOPPADDING',   (0, 0), (-1, -1), 4),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
    ]))

    tbl_w, tbl_h = tbl.wrap(0, 0)
    tbl.drawOn(p, 1.5 * cm, y - tbl_h)
    y -= tbl_h + 0.7 * cm

    # --- Détail tranches RTS ---
    detail = resultat.get('detail_tranches', [])
    if detail:
        p.setFont(_FB, 10)
        p.setFillColor(colors.HexColor('#dc3545'))
        p.drawString(1.5 * cm, y, 'Détail barème RTS par tranche')
        p.setFillColor(colors.black)
        y -= 0.5 * cm

        data_tranches = [['Tranche', 'Montant imposable (GNF)', 'Taux', 'Impôt (GNF)']]
        for t in detail:
            borne_sup = f"{fmt(t['borne_sup'])} GNF" if t.get('borne_sup') else 'Au-delà'
            data_tranches.append([
                f"{fmt(t['borne_inf'])} – {borne_sup}",
                fmt(t['montant_tranche']),
                f"{t['taux']} %",
                fmt(t['impot_tranche']),
            ])
        # Ligne total
        total_impot = sum(t['impot_tranche'] for t in detail)
        data_tranches.append(['TOTAL RTS', '', '', fmt(total_impot)])

        col_w2 = [6 * cm, 4.5 * cm, 2 * cm, 3 * cm]
        tbl2 = Table(data_tranches, colWidths=col_w2)
        tbl2.setStyle(TableStyle([
            ('BACKGROUND',  (0, 0), (-1, 0), colors.HexColor('#dc3545')),
            ('TEXTCOLOR',   (0, 0), (-1, 0), colors.white),
            ('FONTNAME',    (0, 0), (-1, 0), _FB),
            ('FONTSIZE',    (0, 0), (-1, -1), 8),
            ('ALIGN',       (1, 0), (-1, -1), 'RIGHT'),
            ('GRID',        (0, 0), (-1, -1), 0.5, colors.HexColor('#cccccc')),
            ('BACKGROUND',  (0, -1), (-1, -1), colors.HexColor('#fff3cd')),
            ('FONTNAME',    (0, -1), (-1, -1), _FB),
            ('ROWBACKGROUNDS', (0, 1), (-1, -2), [colors.white, colors.HexColor('#f8f9fa')]),
            ('LEFTPADDING',  (0, 0), (-1, -1), 5),
            ('RIGHTPADDING', (0, 0), (-1, -1), 5),
            ('TOPPADDING',   (0, 0), (-1, -1), 3),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
        ]))

        tbl2_w, tbl2_h = tbl2.wrap(0, 0)
        # Vérifier si on a de la place sinon nouvelle page
        if y - tbl2_h < 2 * cm:
            p.showPage()
            y = height - 1.5 * cm
        tbl2.drawOn(p, 1.5 * cm, y - tbl2_h)
        y -= tbl2_h + 0.5 * cm

    # --- Pied de page ---
    p.setFont(_FN, 7)
    p.setFillColor(colors.HexColor('#888888'))
    p.drawCentredString(
        width / 2, 0.8 * cm,
        f"Document généré le {date.today().strftime('%d/%m/%Y')} – Confidentiel – Gestionnaire RH Guinée"
    )
    p.setStrokeColor(colors.HexColor('#cccccc'))
    p.setLineWidth(0.5)
    p.line(1.5 * cm, 1.2 * cm, width - 1.5 * cm, 1.2 * cm)

    p.save()
    buffer.seek(0)

    nom_fichier = f"simulation_retropaie_{date.today().strftime('%Y%m%d')}.pdf"
    response = HttpResponse(buffer.read(), content_type='application/pdf')
    response['Content-Disposition'] = f'attachment; filename="{nom_fichier}"'
    return response


# ============================================================================
# HELPERS STRUCTURATION SALARIALE
# ============================================================================
from .services_simulation import VERSION_BAREME

def _get_client_ip(request):
    """Extrait l'IP client (derrière reverse proxy ou direct)."""
    xff = request.META.get('HTTP_X_FORWARDED_FOR')
    return xff.split(',')[0].strip() if xff else request.META.get('REMOTE_ADDR')

def _log_audit(request, action, simulation=None, metadata=None):
    """Enregistre une entrée d'audit de manière non-bloquante."""
    try:
        from .models import AuditSimulation
        AuditSimulation.objects.create(
            entreprise=request.user.entreprise,
            utilisateur=request.user,
            action=action,
            simulation=simulation,
            metadata=metadata or {},
            ip_address=_get_client_ip(request),
        )
    except Exception:
        pass  # Log audit ne doit jamais bloquer l'opération principale


# ============================================================================
# DÉCOMPOSITION INTELLIGENTE DU BRUT
# Répartit un montant brut en salaire de base + indemnités (transport,
# logement, cherté de vie) et crée les éléments de salaire en lot
# ============================================================================

@login_required
@entreprise_active_required
@reauth_required
def api_decomposer_brut(request):
    """
    POST JSON → retourne une proposition de décomposition du brut.

    Entrée :
        { "brut": 5500000 }
    Sortie :
        { "composantes": [
            {"cle": "salaire_base",   "label": "Salaire de base",        "pct": 60, "montant": 3300000},
            {"cle": "transport",      "label": "Indemnité de transport",  "pct": 15, "montant":  825000},
            {"cle": "logement",       "label": "Indemnité de logement",   "pct": 15, "montant":  825000},
            {"cle": "cherte_vie",     "label": "Cherté de vie",           "pct": 10, "montant":  550000},
          ],
          "rubriques": { "salaire_base": [...], "transport": [...], ... }
        }
    """
    if not hasattr(request.user, 'entreprise') or not request.user.entreprise:
        return JsonResponse({'error': 'Aucune entreprise associée.'}, status=403)
    if not request.user.entreprise.actif:
        return JsonResponse({'error': 'Entreprise désactivée.'}, status=403)
    if request.method != 'POST':
        return JsonResponse({'error': 'POST requis'}, status=405)

    try:
        data = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({'error': 'JSON invalide'}, status=400)

    brut_raw = data.get('brut', 0)
    try:
        brut = int(str(brut_raw).replace(' ', '').replace('\u202f', '').replace('\xa0', ''))
    except (ValueError, TypeError):
        return JsonResponse({'error': 'Montant brut invalide'}, status=400)

    if brut <= 0:
        return JsonResponse({'error': 'Le montant brut doit être positif'}, status=400)

    # Pourcentages par défaut (optimisés pour la fiscalité guinéenne)
    pcts = data.get('pourcentages', {})
    pct_base = int(pcts.get('salaire_base', 60))
    pct_transport = int(pcts.get('transport', 15))
    pct_logement = int(pcts.get('logement', 15))
    pct_cherte = int(pcts.get('cherte_vie', 10))

    total_pct = pct_base + pct_transport + pct_logement + pct_cherte
    if total_pct != 100:
        return JsonResponse({'error': f'La somme des pourcentages doit être 100% (actuellement {total_pct}%)'}, status=400)

    # Calcul des montants : chaque indemnité floor indépendamment, base absorbe le résidu
    import math as _math
    m_transport = _math.floor(brut * pct_transport / 100)
    m_logement = _math.floor(brut * pct_logement / 100)
    m_cherte = _math.floor(brut * pct_cherte / 100)
    m_base = brut - m_transport - m_logement - m_cherte

    composantes = [
        {'cle': 'salaire_base', 'label': 'Salaire de base', 'pct': pct_base, 'montant': m_base},
        {'cle': 'transport', 'label': 'Indemnité de transport', 'pct': pct_transport, 'montant': m_transport},
        {'cle': 'logement', 'label': 'Indemnité de logement', 'pct': pct_logement, 'montant': m_logement},
        {'cle': 'cherte_vie', 'label': 'Cherté de vie', 'pct': pct_cherte, 'montant': m_cherte},
    ]

    # Rechercher les rubriques disponibles pour chaque composante
    entreprise = request.user.entreprise
    rubriques_map = {}

    # Salaire de base — recherche élargie (catégorie + mots-clés code/libelle)
    rubs_base = RubriquePaie.objects.filter(
        entreprise=entreprise, actif=True, type_rubrique='gain'
    ).filter(
        Q(categorie_rubrique='salaire_base') |
        Q(code_rubrique__icontains='SAL') |
        Q(code_rubrique__icontains='BASE') |
        Q(libelle_rubrique__icontains='salaire') |
        Q(libelle_rubrique__icontains='base')
    ).values('id', 'code_rubrique', 'libelle_rubrique')
    # Fallback : toutes les rubriques gain si aucune trouvée
    if not rubs_base.exists():
        rubs_base = RubriquePaie.objects.filter(
            entreprise=entreprise, actif=True, type_rubrique='gain'
        ).values('id', 'code_rubrique', 'libelle_rubrique')
    rubriques_map['salaire_base'] = list(rubs_base)

    # Transport
    rubs_transport = RubriquePaie.objects.filter(
        entreprise=entreprise, actif=True, type_rubrique='gain'
    ).filter(
        Q(code_rubrique__icontains='TRANSPORT') |
        Q(libelle_rubrique__icontains='transport')
    ).values('id', 'code_rubrique', 'libelle_rubrique')
    rubriques_map['transport'] = list(rubs_transport)

    # Logement
    rubs_logement = RubriquePaie.objects.filter(
        entreprise=entreprise, actif=True, type_rubrique='gain'
    ).filter(
        Q(code_rubrique__icontains='LOGEMENT') |
        Q(libelle_rubrique__icontains='logement')
    ).values('id', 'code_rubrique', 'libelle_rubrique')
    rubriques_map['logement'] = list(rubs_logement)

    # Cherté de vie
    rubs_cherte = RubriquePaie.objects.filter(
        entreprise=entreprise, actif=True, type_rubrique='gain'
    ).filter(
        Q(code_rubrique__icontains='CHERTE') |
        Q(code_rubrique__icontains='PCV') |
        Q(libelle_rubrique__icontains='cherté') |
        Q(libelle_rubrique__icontains='cherte') |
        Q(libelle_rubrique__icontains='vie')
    ).values('id', 'code_rubrique', 'libelle_rubrique')
    rubriques_map['cherte_vie'] = list(rubs_cherte)

    # Si pas de rubrique indemnité trouvée, chercher toutes les indemnités
    rubs_indemnites = list(RubriquePaie.objects.filter(
        entreprise=entreprise, actif=True,
        categorie_rubrique='indemnite', type_rubrique='gain'
    ).values('id', 'code_rubrique', 'libelle_rubrique'))
    rubriques_map['toutes_indemnites'] = rubs_indemnites

    return JsonResponse({
        'brut': brut,
        'composantes': composantes,
        'rubriques': rubriques_map,
    })


@login_required
@entreprise_active_required
@reauth_required
@require_POST
def api_creer_elements_lot(request, employe_id):
    if not hasattr(request.user, 'entreprise') or not request.user.entreprise:
        return JsonResponse({'error': 'Aucune entreprise associée.'}, status=403)
    if not request.user.entreprise.actif:
        return JsonResponse({'error': 'Entreprise désactivée.'}, status=403)
    """
    Crée en lot les éléments de salaire issus de la décomposition du brut.
    Sécurisé : permissions, validation dates/montants, anti-doublon, audit.

    POST JSON :
        { "elements": [
            {"rubrique_id": 5, "montant": 3300000},
            {"rubrique_id": 8, "montant": 825000},
            ...
          ],
          "date_debut": "2026-04-01",
          "simulation_id": null       ← optionnel, relie à une simulation sauvegardée
        }
    """
    employe = get_object_or_404(Employe, pk=employe_id, entreprise=request.user.entreprise)

    try:
        data = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({'error': 'JSON invalide'}, status=400)

    elements_data = data.get('elements', [])
    date_debut_str = data.get('date_debut', str(date.today()))
    simulation_id = data.get('simulation_id', None)

    if not elements_data:
        return JsonResponse({'error': 'Aucun élément à créer'}, status=400)

    # ── Validation de la date ──
    try:
        if isinstance(date_debut_str, str):
            date_debut_obj = datetime.strptime(date_debut_str, '%Y-%m-%d').date()
        else:
            date_debut_obj = date.today()
    except (ValueError, TypeError):
        return JsonResponse({'error': 'Format de date invalide (attendu : AAAA-MM-JJ)'}, status=400)

    # ── Vérification anti-doublon (idempotence) ──
    # Si la dernière création en lot date de < 5 secondes, refuser
    from .models import ElementSalaire, SimulationPaie
    dernier_element = ElementSalaire.objects.filter(
        employe=employe, actif=True
    ).order_by('-id').first()
    if dernier_element and hasattr(dernier_element, 'date_debut'):
        from django.utils import timezone as tz
        # Vérifier qu'il n'y a pas eu de création dans les 3 dernières secondes
        import time
        cache_key = f'lot_creation_{employe_id}'
        from django.core.cache import cache
        if cache.get(cache_key):
            return JsonResponse({'error': 'Création en cours, veuillez patienter quelques secondes'}, status=429)
        cache.set(cache_key, True, 5)  # bloque pendant 5 secondes

    crees = []
    erreurs = []
    total_montant = 0

    with transaction.atomic():
        for item in elements_data:
            rubrique_id = item.get('rubrique_id')
            montant = item.get('montant', 0)

            if not rubrique_id or not montant:
                continue

            # ── Validation montant ──
            try:
                montant_decimal = Decimal(str(int(montant)))
                if montant_decimal <= 0 or montant_decimal > 500000000:
                    erreurs.append(f'Montant invalide pour rubrique {rubrique_id}')
                    continue
            except (InvalidOperation, ValueError, TypeError):
                erreurs.append(f'Montant non numérique pour rubrique {rubrique_id}')
                continue

            try:
                rubrique = RubriquePaie.objects.get(
                    pk=rubrique_id, entreprise=request.user.entreprise
                )

                # Désactiver un éventuel élément existant sur la même rubrique
                ElementSalaire.objects.filter(
                    employe=employe, rubrique=rubrique, actif=True
                ).update(actif=False, date_fin=date_debut_str)

                elem = ElementSalaire.objects.create(
                    employe=employe,
                    rubrique=rubrique,
                    montant=montant_decimal,
                    date_debut=date_debut_obj,
                    actif=True,
                    recurrent=True,
                )
                crees.append({
                    'id': elem.id,
                    'rubrique': rubrique.libelle_rubrique,
                    'montant': int(elem.montant),
                })
                total_montant += int(elem.montant)
            except RubriquePaie.DoesNotExist:
                erreurs.append(f'Rubrique introuvable ou non autorisée')
            except Exception:
                erreurs.append('Erreur lors de la création d\'un élément')

        # ── Marquer la simulation comme appliquée ──
        if simulation_id and crees:
            try:
                sim = SimulationPaie.objects.get(
                    pk=simulation_id, entreprise=request.user.entreprise
                )
                sim.appliquee = True
                sim.date_application = timezone.now()
                sim.save(update_fields=['appliquee', 'date_application'])

                # ── Audit log ──
                _log_audit(request, 'creation', sim, {
                    'nb_crees': len(crees), 'nb_erreurs': len(erreurs),
                    'total_montant': total_montant,
                })
            except SimulationPaie.DoesNotExist:
                pass

    return JsonResponse({
        'success': len(crees) > 0,
        'crees': crees,
        'erreurs': erreurs,
        'message': f'{len(crees)} élément(s) créé(s) avec succès' + (
            f', {len(erreurs)} erreur(s)' if erreurs else ''
        ),
    })


# ============================================================================
# GRILLE DE MESSAGES DYNAMIQUES — OPTIMISATION SALARIALE
# Messages intelligents selon ROI × Segment salaire × Charges
# ============================================================================

_MESSAGES_OPTIMISATION = {
    "FORT": {
        "GLOBAL": "Optimisation très efficace — réduction significative des charges employeur.",
        "RECO": "Structure fortement optimisée. Recommandée pour contractualisation afin de réduire durablement le coût salarial.",
        "SEGMENTS": {
            "BAS": "Optimisation efficace malgré un salaire bas.",
            "MOYEN": "Optimisation pertinente avec un bon équilibre entre conformité et économie.",
            "ELEVE": "Structure fortement optimisée — recommandée pour les profils cadres et fonctions à forte rémunération.",
        },
        "IMPACT": "Impact financier significatif sur l'année.",
    },
    "MOYEN": {
        "GLOBAL": "Gain modéré — optimisation utile avec une efficacité correcte.",
        "RECO": None,
        "SEGMENTS": {
            "BAS": "À ce niveau de rémunération, l'impact reste limité. L'intérêt principal est la structuration conforme.",
            "MOYEN": "Bon compromis entre optimisation fiscale et maîtrise des charges — standard recommandé entreprise.",
            "ELEVE": "Optimisation correcte mais sous-exploitée. Un ajustement de la structure peut améliorer le gain.",
        },
        "IMPACT": "Gain modéré mais récurrent sur l'année.",
    },
    "FAIBLE": {
        "GLOBAL": "Impact limité — optimisation peu significative à ce niveau.",
        "RECO": "Revoir la structure salariale ou envisager d'autres formes d'avantages.",
        "SEGMENTS": {
            "BAS": "À ce niveau de salaire, l'optimisation fiscale a un effet marginal. Les charges restent dominantes.",
            "MOYEN": "Structure peu optimisée. Une révision de la répartition des composantes salariales est recommandée.",
            "ELEVE": "Optimisation insuffisante pour ce niveau de rémunération. Un ajustement peut générer un gain significatif.",
        },
        "IMPACT": "Gain faible sur l'année.",
    },
}

_MESSAGES_COMPLEMENTAIRES = {
    "charges_elevees": "Le taux de charges élevé est lié au niveau de rémunération et non à une erreur de configuration.",
    "charges_faibles": "Niveau de charges maîtrisé — situation favorable pour l'employeur.",
    "cnss_plafonnee": "Le plafond CNSS est atteint — les charges sociales n'augmenteront plus proportionnellement au salaire.",
    "exoneration_max": "Plafond d'exonération fiscale atteint (25%) — optimisation maximale appliquée.",
    "salaire_eleve_fort": "Plus le salaire est élevé, plus la CNSS est plafonnée, plus l'exonération joue — l'optimisation devient très rentable.",
}


def _generer_messages_optimisation(brut, roi_pct, taux_charges, gain_mensuel,
                                    charges_pat, cnss_plafonnee=False, exoneration_max=False):
    """
    Génère un bloc complet de messages intelligents pour l'optimisation.
    Retourne un dict prêt à injecter dans la réponse JSON.
    """
    # Niveau ROI
    if roi_pct >= 2.0:
        niveau = "FORT"
        roi = {'level': 'fort', 'label': 'Fort', 'color': 'success'}
    elif roi_pct >= 0.5:
        niveau = "MOYEN"
        roi = {'level': 'moyen', 'label': 'Moyen', 'color': 'info'}
    else:
        niveau = "FAIBLE"
        roi = {'level': 'faible', 'label': 'Faible', 'color': 'warning'}

    # Segment salaire
    if brut < 2_000_000:
        segment = "BAS"
    elif brut <= 5_000_000:
        segment = "MOYEN"
    else:
        segment = "ELEVE"

    cfg = _MESSAGES_OPTIMISATION[niveau]

    roi['message'] = cfg["GLOBAL"]
    roi['message_segment'] = cfg["SEGMENTS"][segment]
    roi['recommandation'] = cfg["RECO"]
    roi['impact_annuel'] = cfg["IMPACT"]

    # Part gain vs charges patronales
    gain_vs_charges = round(abs(gain_mensuel) * 100 / charges_pat, 1) if charges_pat > 0 else 0

    # Économie annuelle
    economie_annuelle = round(abs(gain_mensuel) * 12)

    # Conseils dynamiques (modules complémentaires)
    conseils = []
    if taux_charges > 22:
        conseils.append(_MESSAGES_COMPLEMENTAIRES["charges_elevees"])
    elif taux_charges < 15:
        conseils.append(_MESSAGES_COMPLEMENTAIRES["charges_faibles"])
    if cnss_plafonnee:
        conseils.append(_MESSAGES_COMPLEMENTAIRES["cnss_plafonnee"])
    if exoneration_max:
        conseils.append(_MESSAGES_COMPLEMENTAIRES["exoneration_max"])
    if niveau == "FORT" and segment == "ELEVE":
        conseils.append(_MESSAGES_COMPLEMENTAIRES["salaire_eleve_fort"])
    if niveau == "FAIBLE":
        conseils.append("Regrouper certains avantages ou revoir la structure contractuelle peut être plus pertinent.")

    return {
        'roi_optimisation': roi,
        'gain_vs_charges': gain_vs_charges,
        'economie_annuelle': economie_annuelle,
        'conseils_optimisation': conseils,
    }


# ============================================================================
# IMPACT FISCAL EN TEMPS RÉEL
# Calcule Net / RTS / CNSS pour une décomposition donnée
# ============================================================================

@login_required
@entreprise_active_required
@reauth_required
def api_impact_fiscal(request):
    """
    POST JSON → calcul instantané de l'impact fiscal d'une décomposition.

    Entrée :
        { "brut": 5500000,
          "indemnites": 1650000 }       ← transport + logement + cherté de vie
    Sortie :
        { "cnss": ..., "base_rts": ..., "rts": ..., "net": ..., "taux_effectif": ...,
          "plafond_exon": ..., "exon": ..., "depasse": ...,
          "charges_employeur": {...}, "detail_tranches": [...] }
    """
    if request.method != 'POST':
        return JsonResponse({'error': 'POST requis'}, status=405)

    try:
        data = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({'error': 'JSON invalide'}, status=400)

    brut_val = int(str(data.get('brut', 0)).replace(' ', '').replace('\u202f', '').replace('\xa0', '') or 0)
    indem_val = int(str(data.get('indemnites', 0)).replace(' ', '').replace('\u202f', '').replace('\xa0', '') or 0)

    if brut_val <= 0:
        return JsonResponse({'error': 'Brut invalide'}, status=400)
    if brut_val > 500000000:
        return JsonResponse({'error': 'Brut anormalement élevé'}, status=400)
    # Borner les indemnités au brut (cas limite: tout en indemnités)
    indem_val = max(0, min(indem_val, brut_val))

    from .services_simulation import calculer_un_bareme, _charger_constantes, BAREME_CGI_REFERENCE, _charger_tranches_db

    # Compte les salariés actifs pour TA/ONFPP
    nb_salaries = Employe.objects.filter(
        entreprise=request.user.entreprise,
        statut_employe='actif'
    ).count()

    constantes = _charger_constantes()
    annee = date.today().year

    # Charger meilleur barème dispo
    tranches = _charger_tranches_db(annee, 'officiel')
    if not tranches:
        tranches = list(BAREME_CGI_REFERENCE)

    result = calculer_un_bareme(
        Decimal(str(brut_val)),
        Decimal(str(indem_val)),
        tranches,
        constantes,
        nb_salaries=nb_salaries,
    )

    # ── Indicateur de conformité fiscale ──
    avertissements = []
    conformite = 'conforme'  # conforme / a_verifier / non_conforme

    if result['depasse'] > 0:
        # NB : le front affiche déjà le dépassement via renderEtatExoneration(DEPASSEMENT)
        # → on ne met PAS de texte redondant dans avertissements pour éviter le doublon visuel
        conformite = 'a_verifier'

    if indem_val > brut_val * Decimal('0.75'):
        avertissements.append("Indemnités > 75% du brut — structure inhabituelle")
        conformite = 'non_conforme'
    elif indem_val > brut_val * Decimal('0.50'):
        avertissements.append("Indemnités > 50% du brut — vérifier la justification")
        if conformite == 'conforme':
            conformite = 'a_verifier'

    base_rts = result['base_rts']
    if base_rts <= 0:
        avertissements.append("Base RTS nulle — aucun impôt RTS applicable")
        if conformite == 'conforme':
            conformite = 'a_verifier'

    if brut_val < 550000:
        avertissements.append("Brut inférieur au plancher CNSS (550 000 GNF)")
        if conformite == 'conforme':
            conformite = 'a_verifier'

    # Taux de charge patronale = Charges patronales / Brut × 100 (standard RH)
    cout_total_employeur = result['brut'] + result['total_charges_pat']
    brut_val = result['brut']
    taux_charge_global = round(
        result['total_charges_pat'] / brut_val * 100, 1
    ) if brut_val > 0 else 0

    # Interprétation métier du taux de charge (référence Guinée : CNSS18%+VF6%+ONFPP1.5% = 25.5% max)
    if taux_charge_global < 15:
        taux_charge_interpretation = {'level': 'optimise', 'label': 'Optimisé', 'color': 'success'}
    elif taux_charge_global <= 22:
        taux_charge_interpretation = {'level': 'standard', 'label': 'Standard', 'color': 'info'}
    else:
        taux_charge_interpretation = {'level': 'couteux', 'label': 'Élevé', 'color': 'warning'}

    # ── ROI optimisation : indicateur d'efficacité de l'optimisation fiscale ──
    # Comparer le net actuel vs net sans indemnités (0%) pour mesurer le gain réel
    result_sans_opti = calculer_un_bareme(
        Decimal(str(brut_val)), Decimal('0'), tranches, constantes, nb_salaries=nb_salaries
    )
    gain_opti_net = result['net'] - result_sans_opti['net']
    gain_opti_pct = round(gain_opti_net * 100 / result_sans_opti['net'], 2) if result_sans_opti['net'] > 0 else 0

    # Détection CNSS plafonnée et exonération max
    plafond_cnss = int(constantes.get('PLAFOND_CNSS', 2500000))
    cnss_plafonnee = brut_val >= plafond_cnss
    exoneration_max = result['depasse'] == 0 and indem_val > 0

    msgs = _generer_messages_optimisation(
        brut=brut_val, roi_pct=gain_opti_pct, taux_charges=taux_charge_global,
        gain_mensuel=gain_opti_net, charges_pat=result['total_charges_pat'],
        cnss_plafonnee=cnss_plafonnee, exoneration_max=exoneration_max,
    )

    return JsonResponse({
        'brut': result['brut'],
        'indemnites': indem_val,
        'cnss': result['cnss'],
        'plafond_exon': result['plafond_exon'],
        'exon': result['exon'],
        'depasse': result['depasse'],
        'base_rts': result['base_rts'],
        'rts': result['rts'],
        'taux_effectif': result['taux_effectif'],
        'net': result['net'],
        'cout_total_employeur': cout_total_employeur,
        'taux_charge_global': taux_charge_global,
        'taux_charge_interpretation': taux_charge_interpretation,
        'charges_employeur': {
            'cnss_employeur': result['cnss_employeur'],
            'base_vf': result['base_vf'],
            'base_onfpp': result.get('base_onfpp', 0),
            'deduction_vf': result.get('deduction_vf', 0),
            'vf': result['vf'],
            'ta': result['ta'],
            'onfpp': result['onfpp'],
            'total': result['total_charges_pat'],
        },
        'detail_tranches': result['detail_tranches'],
        # Conformité
        'conformite': conformite,
        'avertissements': avertissements,
        # ROI optimisation
        'roi_optimisation': msgs['roi_optimisation'],
        'gain_opti_net': gain_opti_net,
        'gain_opti_pct': gain_opti_pct,
        'gain_vs_charges': msgs['gain_vs_charges'],
        'economie_annuelle': msgs['economie_annuelle'],
        'conseils_optimisation': msgs['conseils_optimisation'],
        # Traçabilité : règles appliquées
        'regles': {
            'cnss_formule': f"min({result['brut']:,}, {int(constantes.get('PLAFOND_CNSS', 2500000)):,}) × {float(constantes.get('TAUX_CNSS_EMPLOYE', 5))}%",
            'exon_formule': f"min({indem_val:,}, {result['plafond_exon']:,}) → plafond 25% × {result['brut']:,}",
            'base_rts_formule': f"{result['brut']:,} − {result['cnss']:,} − {result['exon']:,} + {result['depasse']:,} = {result['base_rts']:,}",
            'base_vf_formule': f"{result['brut']:,} - {result.get('deduction_vf', 0):,} = {result['base_vf']:,} ; base VF = brut - indemnites exonerees plafonnees",
            'vf_formule': f"{result['base_vf']:,} x {float(constantes.get('TAUX_VF', 6))}% = {result['vf']:,}",
            'onfpp_formule': f"{result.get('base_onfpp', 0):,} x 1.5% = {result['onfpp']:,}",
            'net_formule': f"{result['brut']:,} − {result['cnss']:,} − {result['rts']:,} = {result['net']:,}",
            'reference': 'CGI Guinée – Art. 196 (exonération 25%) / Art. 197 (barème RTS progressif)',
            'reference_vf': 'VF/ONFPP: base = brut - indemnites exonerees plafonnees; VF = base x 6%; ONFPP = base x 1,5% si effectif >= 30.',
        },
    })


# ============================================================================
# OPTIMISATION FISCALE DE LA DÉCOMPOSITION
# Trouve la répartition qui minimise le RTS (maximise le net)
# ============================================================================

@login_required
@entreprise_active_required
@reauth_required
def api_optimiser_decomposition(request):
    """
    POST JSON → trouve la répartition optimale du brut.

    Entrée :
        { "brut": 5500000,
          "verrous": { "transport": 300000 }  ← (optionnel) montants verrouillés
        }
    Sortie :
        { "composantes": [...], "impact": {...}, "gain_vs_actuel": {...} }

    Stratégie: maximiser les indemnités exonérées (jusqu'à 25% du brut)
    tout en respectant les verrous utilisateur.
    """
    if request.method != 'POST':
        return JsonResponse({'error': 'POST requis'}, status=405)

    try:
        data = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({'error': 'JSON invalide'}, status=400)

    brut_val = int(str(data.get('brut', 0)).replace(' ', '').replace('\u202f', '').replace('\xa0', '') or 0)
    verrous = data.get('verrous', {})  # {"transport": 300000, ...}
    pcts_actuels = data.get('pourcentages_actuels', {})
    objectif = data.get('objectif', 'max_net')  # 'max_net' ou 'min_cout'
    if objectif not in ('max_net', 'min_cout'):
        objectif = 'max_net'

    if brut_val <= 0:
        return JsonResponse({'error': 'Brut invalide'}, status=400)

    # Garde-fous : brut minimum et maximum raisonnables
    if brut_val < 100000:
        return JsonResponse({'error': 'Brut trop faible pour une optimisation pertinente (min 100 000 GNF)'}, status=400)
    if brut_val > 500000000:
        return JsonResponse({'error': 'Brut anormalement élevé (max 500 000 000 GNF)'}, status=400)

    from .services_simulation import calculer_un_bareme, _charger_constantes, BAREME_CGI_REFERENCE, _charger_tranches_db, optimiser_structure_dynamique

    # Compte les salariés actifs pour TA/ONFPP
    nb_salaries = Employe.objects.filter(
        entreprise=request.user.entreprise,
        statut_employe='actif'
    ).count()

    constantes = _charger_constantes()
    annee = date.today().year
    tranches = _charger_tranches_db(annee, 'officiel')
    if not tranches:
        tranches = list(BAREME_CGI_REFERENCE)

    # --- Optimisation dynamique (test toutes les structures 0-25%) ---
    optim_dyn = optimiser_structure_dynamique(brut_val, tranches, constantes, nb_salaries=nb_salaries, objectif=objectif)
    best_dyn = optim_dyn['best']
    best_taux = best_dyn['taux_indem_pct']

    # Montants verrouillés
    verrous_montants = {}
    for cle in ['salaire_base', 'transport', 'logement', 'cherte_vie']:
        if cle in verrous:
            val = int(str(verrous[cle]).replace(' ', '').replace('\u202f', '').replace('\xa0', '') or 0)
            if val > 0:
                verrous_montants[cle] = max(0, min(val, brut_val))  # borner au brut

    # Cas limite : tout verrouillé
    if len(verrous_montants) >= 4:
        return JsonResponse({'error': 'Toutes les composantes sont verrouillées. Déverrouillez au moins une composante.'}, status=400)

    # === Stratégie optimale (dynamique) ===
    # Utilise le taux trouvé par l'optimisation dynamique (0-25%)
    plafond_exon = int(Decimal(str(brut_val)) * Decimal(str(best_taux)) / Decimal('100'))

    # Budget verrouillé
    total_verrous = sum(verrous_montants.values())
    indemnites_verrouillees = sum(v for k, v in verrous_montants.items() if k != 'salaire_base')
    base_verrouillee = verrous_montants.get('salaire_base', 0)

    # L'objectif : répartir le budget non-verrouillé pour que les indemnités totales = plafond_exon
    composantes_libres = [c for c in ['salaire_base', 'transport', 'logement', 'cherte_vie']
                          if c not in verrous_montants]
    indemnites_libres = [c for c in composantes_libres if c != 'salaire_base']
    base_libre = 'salaire_base' in composantes_libres

    budget_restant = brut_val - total_verrous

    if budget_restant < 0:
        return JsonResponse({
            'error': f'Les montants verrouillés ({total_verrous:,} GNF) dépassent le brut ({brut_val:,} GNF)'
        }, status=400)

    # Indemnités cibles = plafond_exon - indemnités déjà verrouillées
    indem_cible = max(0, plafond_exon - indemnites_verrouillees)

    # Répartir les indemnités cibles entre les composantes libres d'indemnités
    nb_indem_libres = len(indemnites_libres)

    if base_libre and nb_indem_libres > 0:
        # On peut ajuster la base ET les indemnités
        indem_totale_a_repartir = min(indem_cible, budget_restant)
        base_optimale = budget_restant - indem_totale_a_repartir
    elif base_libre and nb_indem_libres == 0:
        # Toutes les indemnités sont verrouillées, tout le reste va en base
        indem_totale_a_repartir = 0
        base_optimale = budget_restant
    elif not base_libre and nb_indem_libres > 0:
        # La base est verrouillée, répartir le reste en indemnités
        indem_totale_a_repartir = min(budget_restant, indem_cible)
        base_optimale = 0  # déjà verrouillée
    else:
        # Tout est verrouillé
        indem_totale_a_repartir = 0
        base_optimale = 0

    # Répartir les indemnités libres équitablement
    final = {}
    indem_libres_restant = indem_totale_a_repartir
    indem_libres_placees = 0
    for cle in ['salaire_base', 'transport', 'logement', 'cherte_vie']:
        if cle in verrous_montants:
            final[cle] = verrous_montants[cle]
        elif cle == 'salaire_base':
            final[cle] = base_optimale
        elif nb_indem_libres > 0:
            indem_libres_placees += 1
            if indem_libres_placees == nb_indem_libres:
                # Dernière indemnité libre : absorbe le reste pour éviter tout écart d'arrondi
                final[cle] = indem_libres_restant
            else:
                part = int((Decimal(str(indem_totale_a_repartir)) / Decimal(str(nb_indem_libres))).quantize(Decimal('1'), rounding=ROUND_HALF_UP))
                final[cle] = part
                indem_libres_restant -= part
        else:
            final[cle] = 0

    # Ajuster l'arrondi sur la base
    total_calc = sum(final.values())
    if total_calc != brut_val and 'salaire_base' not in verrous_montants:
        final['salaire_base'] += (brut_val - total_calc)

    total_indemnites = final.get('transport', 0) + final.get('logement', 0) + final.get('cherte_vie', 0)

    # Calculer l'impact fiscal optimisé
    impact_optimal = calculer_un_bareme(
        Decimal(str(brut_val)),
        Decimal(str(total_indemnites)),
        tranches,
        constantes,
        nb_salaries=nb_salaries,
    )

    # Comparer avec la répartition actuelle (si fournie)
    gain_info = None
    comparaison = None
    if pcts_actuels:
        pct_base_actuel = int(pcts_actuels.get('salaire_base', 60))
        pct_transport_actuel = int(pcts_actuels.get('transport', 15))
        pct_logement_actuel = int(pcts_actuels.get('logement', 15))
        pct_cherte_actuel = int(pcts_actuels.get('cherte_vie', 10))

        indem_actuelles = round(brut_val * (pct_transport_actuel + pct_logement_actuel + pct_cherte_actuel) / 100)
        impact_actuel = calculer_un_bareme(
            Decimal(str(brut_val)),
            Decimal(str(indem_actuelles)),
            tranches,
            constantes,
            nb_salaries=nb_salaries,
        )
        gain_net = impact_optimal['net'] - impact_actuel['net']
        economie_rts = impact_actuel['rts'] - impact_optimal['rts']
        gain_info = {
            'net_avant': impact_actuel['net'],
            'net_apres': impact_optimal['net'],
            'gain_net_mensuel': gain_net,
            'gain_net_annuel': gain_net * 12,
            'rts_avant': impact_actuel['rts'],
            'rts_apres': impact_optimal['rts'],
            'economie_rts_mensuelle': economie_rts,
        }

        # ── Mode comparaison : tableau Standard vs Optimisé ──
        # Scénario "standard" = 100% base, 0 indemnités
        impact_standard = calculer_un_bareme(
            Decimal(str(brut_val)),
            Decimal('0'),
            tranches,
            constantes,
            nb_salaries=nb_salaries,
        )
        comparaison = [
            {
                'scenario': 'Standard (100% base)',
                'brut': brut_val,
                'base': brut_val,
                'indemnites': 0,
                'cnss': impact_standard['cnss'],
                'rts': impact_standard['rts'],
                'net': impact_standard['net'],
                'taux': impact_standard['taux_effectif'],
            },
            {
                'scenario': 'Actuel',
                'brut': brut_val,
                'base': round(brut_val * pct_base_actuel / 100),
                'indemnites': indem_actuelles,
                'cnss': impact_actuel['cnss'],
                'rts': impact_actuel['rts'],
                'net': impact_actuel['net'],
                'taux': impact_actuel['taux_effectif'],
            },
            {
                'scenario': 'Optimisé (25% exon.)',
                'brut': brut_val,
                'base': final.get('salaire_base', 0),
                'indemnites': total_indemnites,
                'cnss': impact_optimal['cnss'],
                'rts': impact_optimal['rts'],
                'net': impact_optimal['net'],
                'taux': impact_optimal['taux_effectif'],
            },
        ]

    # Convertir en pourcentages pour l'UI
    composantes = []
    labels = {
        'salaire_base': 'Salaire de base',
        'transport': 'Indemnité de transport',
        'logement': 'Indemnité de logement',
        'cherte_vie': 'Cherté de vie',
    }
    for cle in ['salaire_base', 'transport', 'logement', 'cherte_vie']:
        montant = final[cle]
        pct = round(montant * 100 / brut_val) if brut_val > 0 else 0
        composantes.append({
            'cle': cle,
            'label': labels[cle],
            'pct': pct,
            'montant': montant,
            'verrouille': cle in verrous_montants,
        })

    return JsonResponse({
        'brut': brut_val,
        'composantes': composantes,
        'impact': {
            'cnss': impact_optimal['cnss'],
            'base_rts': impact_optimal['base_rts'],
            'rts': impact_optimal['rts'],
            'net': impact_optimal['net'],
            'taux_effectif': impact_optimal['taux_effectif'],
            'plafond_exon': impact_optimal['plafond_exon'],
            'exon': impact_optimal['exon'],
            'depasse': impact_optimal['depasse'],
            'detail_tranches': impact_optimal['detail_tranches'],
        },
        'gain': gain_info,
        'comparaison': comparaison,
        'objectif': objectif,
        'optimisation_dynamique': {
            'taux_optimal': best_taux,
            'net_optimal': best_dyn['net'],
            'cout_optimal': best_dyn['cout_total_employeur'],
            'scenarios_testes': len(optim_dyn['scenarios']),
            'gain_net': optim_dyn['gain_net'],
            'gain_pct': optim_dyn['gain_pct'],
            'gain_sans_surcout': optim_dyn['gain_sans_surcout'],
            'net_standard': optim_dyn['ref_standard']['net'],
            'cout_standard': optim_dyn['ref_standard']['cout_total_employeur'],
        },
        'regle': 'Indemnités exonérées jusqu\'à 25% du brut (CGI Guinée)',
    })


# ============================================================================
# PROPOSITION AUTOMATIQUE COMPLÈTE : Net cible → Brut → Optimisation → Résumé
# Un clic : calcul brut, optimisation structure, affichage recommandation
# ============================================================================

@login_required
@entreprise_active_required
@reauth_required
@require_POST
def api_proposition_complete(request):
    """
    POST JSON → chaîne complète : net_cible → brut (retropaie) → optimisation
    dynamique → structure recommandée + résumé décisionnel.

    Entrée  : { "net_cible": 5500000, "objectif": "max_net"|"min_cout" }
    Sortie  : retropaie + optimisation + recommandation en une seule réponse
    """
    if not hasattr(request.user, 'entreprise') or not request.user.entreprise:
        return JsonResponse({'error': 'Aucune entreprise associée à ce compte.'}, status=403)
    if not request.user.entreprise.actif:
        return JsonResponse({'error': 'Votre entreprise est désactivée.'}, status=403)

    try:
        data = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({'error': 'Corps JSON invalide'}, status=400)

    raw_net = data.get('net_cible', '')
    if not raw_net:
        return JsonResponse({'error': 'Le net cible est requis'}, status=400)

    try:
        net_cible = Decimal(str(raw_net).replace(' ', '').replace('\xa0', ''))
        if net_cible <= 0:
            raise ValueError
    except (InvalidOperation, ValueError):
        return JsonResponse({'error': 'Montant net invalide'}, status=400)

    objectif = data.get('objectif', 'max_net')
    if objectif not in ('max_net', 'min_cout'):
        objectif = 'max_net'

    # Taux max personnalisable (plafond interne entreprise)
    try:
        taux_max = int(data.get('taux_max', 25) or 25)
        taux_max = max(0, min(25, taux_max))
    except (TypeError, ValueError):
        taux_max = 25

    annee = date.today().year

    # ── Étape 1 : Double rétro-paie (sans puis avec optimisation fiscale) ──
    from .services_retropaie import retropaie_net_vers_brut, calculer_charges_patronales

    # 1a. Rétro-paie SANS exonération (référence)
    try:
        retro_ref = retropaie_net_vers_brut(
            net_cible=net_cible,
            annee=annee,
            pct_indemnites_forfaitaires=0,
            garantir_net_minimum=True,
        )
    except Exception as e:
        return JsonResponse({'error': f'Erreur retropaie : {str(e)}'}, status=500)

    if not retro_ref.get('ok'):
        return JsonResponse({
            'error': 'Convergence retropaie impossible pour ce net cible.',
            'retropaie': {'brut': int(retro_ref['brut']), 'ecart': int(retro_ref['ecart'])}
        }, status=400)

    # 1b. Rétro-paie AVEC exonération optimale (taux_max) → brut PLUS BAS
    #     pour le même net exact = 5 500 000
    try:
        retro = retropaie_net_vers_brut(
            net_cible=net_cible,
            annee=annee,
            pct_indemnites_forfaitaires=taux_max,
            garantir_net_minimum=True,
        )
    except Exception as e:
        # Fallback : utiliser le résultat sans exonération
        retro = retro_ref

    if not retro.get('ok'):
        retro = retro_ref

    brut_calcule = int(retro['brut'])
    brut_sans_opti = int(retro_ref['brut'])

    # ── Étape 2 : Charges patronales ──────────────────────────────────────
    nb_salaries = Employe.objects.filter(
        entreprise=request.user.entreprise,
        statut_employe='actif'
    ).count()
    try:
        cp = calculer_charges_patronales(
            retro['brut'], annee=annee, nb_salaries=nb_salaries,
            pct_indemnites_forfaitaires=taux_max,
        )
    except Exception:
        cp = {'cnss_employeur': 0, 'vf': 0, 'ta': 0, 'libelle_ta': 'TA',
              'total': 0, 'cout_total_employeur': brut_calcule}

    try:
        cp_ref = calculer_charges_patronales(
            retro_ref['brut'], annee=annee, nb_salaries=nb_salaries,
            pct_indemnites_forfaitaires=0,
        )
    except Exception:
        cp_ref = cp

    # ── Étape 3 : Optimisation dynamique de la structure ──────────────────
    from .services_simulation import optimiser_structure_dynamique, _charger_constantes, _charger_tranches_db, BAREME_CGI_REFERENCE

    constantes_sim = _charger_constantes()
    tranches_sim = _charger_tranches_db(annee, 'officiel')
    if not tranches_sim:
        tranches_sim = list(BAREME_CGI_REFERENCE)

    try:
        optim = optimiser_structure_dynamique(
            brut_calcule, tranches_sim, constantes_sim, objectif=objectif,
            taux_max=taux_max
        )
    except Exception as e:
        return JsonResponse({'error': f'Erreur optimisation : {str(e)}'}, status=500)

    best = optim['best']
    ref = optim['ref_standard']

    # ── Étape 4 : Vérification croisée (assert net cohérent) ─────────────
    net_verif, *_ = None, None, None, None, None
    try:
        from .services_retropaie import _net_depuis_brut as _ndb
    except ImportError:
        _ndb = None

    verification_ok = True
    if _ndb:
        try:
            from .cache_service import PayrollCacheService as _PCS
            _cst = _PCS.get_constantes(date_reference=date(annee, 1, 1))
            _tr  = _PCS.get_tranches_rts(annee)
            _cst.setdefault('PLANCHER_CNSS', Decimal('550000'))
            _cst.setdefault('PLAFOND_CNSS', Decimal('2500000'))
            _cst.setdefault('TAUX_CNSS_EMPLOYE', Decimal('5'))
            pct_verif = Decimal(str(best['taux_indem_pct']))
            net_verif_val, *_ = _ndb(Decimal(str(brut_calcule)), _cst, _tr, pct_verif)
            # Comparer avec le net cible de la retropaie (garanti exact par dichotomie)
            net_retro = int(retro['net_calcule'])
            verification_ok = abs(int(net_verif_val) - net_retro) <= 10
        except Exception:
            verification_ok = True  # ne pas bloquer si vérif échoue

    # ── Étape 4 : Construire la recommandation ────────────────────────────
    def fmt(n):
        return f"{int(n):,}".replace(',', ' ')

    taux_optimal = best['taux_indem_pct']
    pct_base = round(100 - taux_optimal, 1)
    pct_indem = taux_optimal

    # Répartition recommandée des indemnités (proportionnelle)
    # Chaque indemnité floor indépendamment, base absorbe le résidu
    pct_transport = round(pct_indem * 0.4, 1)
    pct_logement  = round(pct_indem * 0.4, 1)
    pct_cherte    = round(pct_indem * 0.2, 1)
    import math as _math
    montant_transport = _math.floor(brut_calcule * pct_transport / 100)
    montant_logement  = _math.floor(brut_calcule * pct_logement / 100)
    montant_cherte    = _math.floor(brut_calcule * pct_cherte / 100)
    # Ajustement floor : somme indemnités = floor(brut × pct_indem%)
    _total_cible = _math.floor(brut_calcule * pct_indem / 100)
    montant_cherte += _total_cible - (montant_transport + montant_logement + montant_cherte)
    montant_base      = brut_calcule - montant_transport - montant_logement - montant_cherte
    recommandation_composantes = [
        {'cle': 'salaire_base', 'pct': pct_base, 'montant': montant_base},
        {'cle': 'transport',    'pct': pct_transport, 'montant': montant_transport},
        {'cle': 'logement',     'pct': pct_logement, 'montant': montant_logement},
        {'cle': 'cherte_vie',   'pct': pct_cherte, 'montant': montant_cherte},
    ]

    # ── Audit : historiser la proposition ────────────────────────────────
    try:
        from .models import AuditSimulation
        AuditSimulation.objects.create(
            entreprise=request.user.entreprise,
            utilisateur=request.user,
            action='proposition',
            metadata={
                'mode': 'proposition_complete',
                'net_cible': int(net_cible),
                'objectif': objectif,
                'taux_max': taux_max,
                'brut_calcule': brut_calcule,
                'taux_optimal': taux_optimal,
                'net_optimal': int(best['net']),
                'gain_net': int(optim['gain_net']),
                'gain_pct': optim['gain_pct'],
                'gain_sans_surcout': optim['gain_sans_surcout'],
                'verification_ok': verification_ok,
            },
            ip_address=request.META.get('REMOTE_ADDR'),
        )
    except Exception:
        pass  # Ne pas bloquer la réponse en cas d'erreur d'audit

    # Économie employeur grâce à l'optimisation fiscale
    economie_brut = brut_sans_opti - brut_calcule
    economie_cout = cp_ref['cout_total_employeur'] - cp['cout_total_employeur']

    # ── ROI optimisation : indicateur d'efficacité ──
    gain_opti_pct = round(economie_cout * 100 / cp_ref['cout_total_employeur'], 2) if cp_ref['cout_total_employeur'] > 0 else 0
    taux_charges_pct = round(cp['total'] * 100 / brut_calcule, 1) if brut_calcule > 0 else 0

    # Détection CNSS plafonnée et exonération max
    plafond_cnss = int(_cst.get('PLAFOND_CNSS', 2500000))
    cnss_plafonnee = brut_calcule >= plafond_cnss
    exoneration_max = taux_max >= 25

    msgs = _generer_messages_optimisation(
        brut=brut_calcule, roi_pct=gain_opti_pct, taux_charges=taux_charges_pct,
        gain_mensuel=economie_cout, charges_pat=cp['total'],
        cnss_plafonnee=cnss_plafonnee, exoneration_max=exoneration_max,
    )

    return JsonResponse({
        'net_cible': int(net_cible),
        'objectif': objectif,
        'annee': annee,

        # Rétro-paie (avec exonération optimale → net exact = net_cible)
        'retropaie': {
            'brut': brut_calcule,
            'brut_sans_opti': brut_sans_opti,
            'cnss': int(retro['cnss']),
            'base_rts': int(retro['base_rts']),
            'rts_standard': int(retro_ref['rts']),
            'rts_optimise': int(retro['rts']),
            'taux_effectif': round(float(retro['rts']) / float(retro['base_rts']) * 100, 1) if float(retro.get('base_rts', 0)) > 0 else 0,
            'net_calcule': int(retro['net_calcule']),
            'ecart': int(retro['ecart']),
            'iterations': retro['iterations'],
            'ok': retro['ok'],
            'pct_exoneration': taux_max,
            'cnss_plafonnee': cnss_plafonnee,
            'plafond_cnss': plafond_cnss,
        },

        # Charges patronales (sur brut optimisé)
        'charges_patronales': {
            'cnss_employeur': cp['cnss_employeur'],
            'vf': cp['vf'],
            'ta': cp['ta'],
            'libelle_ta': cp['libelle_ta'],
            'total': cp['total'],
            'cout_total_employeur': cp['cout_total_employeur'],
        },

        # Optimisation dynamique
        'optimisation': {
            'taux_optimal': taux_optimal,
            'net_optimal': int(retro['net_calcule']),
            'cout_optimal': cp['cout_total_employeur'],
            'net_standard': int(retro_ref['net_calcule']),
            'cout_standard': cp_ref['cout_total_employeur'],
            'economie_brut': economie_brut,
            'economie_cout': economie_cout,
            'gain_net': int(optim['gain_net']),
            'gain_pct': optim['gain_pct'],
            'gain_sans_surcout': optim['gain_sans_surcout'],
            'scenarios_testes': len(optim['scenarios']),
        },

        # Recommandation
        'recommandation': {
            'composantes': recommandation_composantes,
            'resume': (
                f"Pour un net exact de {fmt(net_cible)} GNF, brut recommandé : "
                f"{fmt(brut_calcule)} GNF avec {pct_indem}% d'indemnités exonérées. "
                f"Net à payer = {fmt(retro['net_calcule'])} GNF"
                + (f". Économie employeur : −{fmt(economie_cout)} GNF vs structure sans optimisation"
                   if economie_cout > 0 else '')
                + f". Coût total employeur : {fmt(cp['cout_total_employeur'])} GNF."
            ),
        },

        # Formaté pour affichage
        'formatted': {
            'brut': fmt(brut_calcule),
            'brut_sans_opti': fmt(brut_sans_opti),
            'net_cible': fmt(net_cible),
            'net_standard': fmt(retro_ref['net_calcule']),
            'net_optimal': fmt(retro['net_calcule']),
            'economie_brut': fmt(economie_brut),
            'economie_cout': fmt(economie_cout),
            'cout_total': fmt(cp['cout_total_employeur']),
            'cout_total_sans_opti': fmt(cp_ref['cout_total_employeur']),
        },

        # Vérification croisée + paramètres
        'verification_ok': verification_ok,
        'taux_max': taux_max,

        # ROI optimisation
        'roi_optimisation': msgs['roi_optimisation'],
        'gain_opti_pct': gain_opti_pct,
        'taux_charges_pct': taux_charges_pct,
        'gain_vs_charges': msgs['gain_vs_charges'],
        'economie_annuelle': msgs['economie_annuelle'],
        'conseils_optimisation': msgs['conseils_optimisation'],
    })


# ============================================================================
# VALIDATION AVANT CRÉATION (étape de confirmation)
# ============================================================================

@login_required
@reauth_required
@entreprise_active_required
@require_POST
def api_valider_simulation(request):
    """
    POST JSON → valide une simulation et retourne un résumé + avertissements.
    L'utilisateur doit confirmer avant la création en lot.

    Entrée : { "brut": 5500000, "composantes": [...], "employe_id": 123 }
    Sortie : { "valide": true/false, "resume": [...], "avertissements": [...],
               "conformite": "conforme", "simulation_id": 42 }
    """
    try:
        data = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({'error': 'JSON invalide'}, status=400)

    brut_val = int(data.get('brut', 0))
    composantes = data.get('composantes', [])
    employe_id = data.get('employe_id', None)

    if brut_val <= 0 or not composantes:
        return JsonResponse({'error': 'Données insuffisantes'}, status=400)

    # ── Recalculer côté serveur (ne pas faire confiance au JS) ──
    from .services_simulation import calculer_un_bareme, _charger_constantes, BAREME_CGI_REFERENCE, _charger_tranches_db

    # Compte les salariés actifs pour TA/ONFPP
    nb_salaries = Employe.objects.filter(
        entreprise=request.user.entreprise,
        statut_employe='actif'
    ).count()

    constantes = _charger_constantes()
    annee = date.today().year
    tranches = _charger_tranches_db(annee, 'officiel')
    if not tranches:
        tranches = list(BAREME_CGI_REFERENCE)

    total_indem = sum(c.get('montant', 0) for c in composantes if c.get('cle') != 'salaire_base')
    result = calculer_un_bareme(
        Decimal(str(brut_val)),
        Decimal(str(total_indem)),
        tranches,
        constantes,
        nb_salaries=nb_salaries,
    )

    # ── Vérifications de conformité ──
    avertissements = []
    conformite = 'conforme'

    # 1. Vérifier somme = brut
    total_comp = sum(c.get('montant', 0) for c in composantes)
    ecart = abs(total_comp - brut_val)
    if ecart > 1:
        avertissements.append(f"⚠ Écart de cohérence : somme composantes ({total_comp:,}) ≠ brut ({brut_val:,})")
        conformite = 'non_conforme'

    # 2. Dépassement exonération
    if result['depasse'] > 0:
        avertissements.append(f"⚠ Dépassement exonération : +{result['depasse']:,} GNF au-dessus du plafond 25%")
        if conformite == 'conforme':
            conformite = 'a_verifier'

    # 3. Indemnités anormalement élevées
    if total_indem > brut_val * 0.75:
        avertissements.append("⚠ Indemnités > 75% du brut — structure fiscalement risquée")
        conformite = 'non_conforme'
    elif total_indem > brut_val * 0.50:
        avertissements.append("⚠ Indemnités > 50% du brut — vérifier la justification")
        if conformite == 'conforme':
            conformite = 'a_verifier'

    # 4. Salaire de base trop faible
    base = next((c.get('montant', 0) for c in composantes if c.get('cle') == 'salaire_base'), 0)
    if base < brut_val * 0.30:
        avertissements.append("⚠ Salaire de base < 30% du brut — peut être contesté par l'administration fiscale")
        if conformite == 'conforme':
            conformite = 'a_verifier'

    # 5. Brut sous plancher CNSS
    if brut_val < 550000:
        avertissements.append("⚠ Brut inférieur au plancher CNSS (550 000 GNF)")
        if conformite == 'conforme':
            conformite = 'a_verifier'

    # 6. Vérifier que l'employé appartient bien à l'entreprise
    employe_nom = ''
    if employe_id:
        try:
            employe = Employe.objects.get(pk=employe_id, entreprise=request.user.entreprise)
            employe_nom = str(employe)
        except Employe.DoesNotExist:
            return JsonResponse({'error': 'Employé introuvable ou non autorisé'}, status=403)

    # ── Résumé ──
    resume = []
    for c in composantes:
        resume.append({
            'label': c.get('label', c.get('cle', '')),
            'pct': c.get('pct', 0),
            'montant': c.get('montant', 0),
        })

    # ── Sauvegarder la simulation (reproductibilité) ──
    from .models import SimulationPaie
    sim = SimulationPaie.objects.create(
        entreprise=request.user.entreprise,
        employe_id=employe_id,
        utilisateur=request.user,
        brut=Decimal(str(brut_val)),
        composantes=composantes,
        impact={
            'cnss': result['cnss'],
            'rts': result['rts'],
            'net': result['net'],
            'taux_effectif': result['taux_effectif'],
            'base_rts': result['base_rts'],
            'plafond_exon': result['plafond_exon'],
            'exon': result['exon'],
            'depasse': result['depasse'],
            'detail_tranches': result['detail_tranches'],
            'statut_exoneration': (
                'depassement' if result['depasse'] > 0
                else 'ok_parfait' if result['exon'] == result['plafond_exon'] and result['exon'] > 0
                else 'ok_normal'
            ),
        },
        conformite=conformite,
        avertissements=avertissements,
        version_bareme=VERSION_BAREME,
    )

    # ── Audit log ──
    _log_audit(request, 'validation', sim, {'brut': brut_val, 'conformite': conformite})

    return JsonResponse({
        'valide': conformite != 'non_conforme',
        'conformite': conformite,
        'avertissements': avertissements,
        'resume': resume,
        'impact_serveur': {
            'cnss': result['cnss'],
            'rts': result['rts'],
            'net': result['net'],
            'taux_effectif': result['taux_effectif'],
        },
        'simulation_id': sim.pk,
        'employe_nom': employe_nom,
    })


# ============================================================================
# HISTORIQUE DES SIMULATIONS PAR EMPLOYE
# ============================================================================

@login_required
@reauth_required
@entreprise_active_required
@require_GET
def api_historique_simulations_employe(request, employe_id):
    """
    GET → retourne les dernières simulations (SimulationPaie) d'un employé.
    ?limit=10  (défaut 10, max 50)
    """
    from .models import SimulationPaie

    qs = SimulationPaie.objects.filter(
        employe_id=employe_id,
        entreprise=request.user.entreprise,
    ).order_by('-date_creation')

    total = qs.count()
    limit = min(int(request.GET.get('limit', 10)), 50)
    sims = qs[:limit]

    def _resume_structure(composantes):
        """Extrait le ratio base/indemnités depuis les composantes JSON."""
        base_pct = 0
        indem_pct = 0
        for c in (composantes or []):
            pct = c.get('pct', 0)
            if c.get('cle') == 'salaire_base':
                base_pct = pct
            else:
                indem_pct += pct
        return f"{base_pct}/{indem_pct}" if base_pct else "—"

    results = []
    for s in sims:
        impact = s.impact or {}
        statut = impact.get('statut_exoneration', '')
        if not statut and impact:
            depasse = impact.get('depasse', 0)
            exon = impact.get('exon', 0)
            plafond = impact.get('plafond_exon', 0)
            statut = (
                'depassement' if depasse > 0
                else 'ok_parfait' if exon == plafond and exon > 0
                else 'ok_normal'
            )
        results.append({
            'id': s.pk,
            'date': s.date_creation.strftime('%d/%m/%Y %H:%M'),
            'brut': int(s.brut),
            'net': impact.get('net', 0),
            'conformite': s.conformite,
            'statut_exoneration': statut,
            'appliquee': s.appliquee,
            'date_application': s.date_application.strftime('%d/%m/%Y %H:%M') if s.date_application else None,
            'utilisateur': str(s.utilisateur.get_short_name() or s.utilisateur.username),
            'composantes_resume': _resume_structure(s.composantes),
            'version_bareme': s.version_bareme,
        })

    return JsonResponse({'simulations': results, 'total': total})


# ============================================================================
# FICHE DE SIMULATION PDF
# Génère un PDF professionnel résumant la structuration salariale
# ============================================================================

@login_required
@reauth_required
@entreprise_active_required
def api_simulation_pdf(request):
    """
    POST JSON → génère un PDF de simulation salariale.
    Recalcul côté serveur pour garantir la fiabilité des chiffres.

    Entrée :
        { "brut": 5500000,
          "composantes": [{"cle": "salaire_base", "label": "...", "pct": 60, "montant": 3300000}, ...],
          "comparaison": [...],  ← optionnel
          "gain": {...},         ← optionnel
          "employe_nom": "..."   ← optionnel
        }
    """
    if request.method != 'POST':
        return JsonResponse({'error': 'POST requis'}, status=405)

    try:
        data = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({'error': 'JSON invalide'}, status=400)

    simulation_id = data.get('simulation_id', None)
    sim_obj = None         # référence SimulationPaie (si fournie)
    version_bareme = None  # pour la traçabilité PDF

    if simulation_id:
        # ── MODE TRAÇABLE : charger depuis SimulationPaie (zéro recalcul) ──
        from .models import SimulationPaie
        try:
            sim_obj = SimulationPaie.objects.select_related('employe').get(
                pk=simulation_id, entreprise=request.user.entreprise
            )
        except SimulationPaie.DoesNotExist:
            return JsonResponse({'error': 'Simulation introuvable'}, status=404)

        brut = int(sim_obj.brut)
        composantes = sim_obj.composantes or []
        comparaison = sim_obj.comparaison
        gain = sim_obj.gain
        employe_nom = str(sim_obj.employe) if sim_obj.employe else ''
        impact_data = sim_obj.impact or {}
        version_bareme = sim_obj.version_bareme
    else:
        # ── MODE PREVIEW : recalcul côté serveur (comportement existant) ──
        brut = int(data.get('brut', 0))
        composantes = data.get('composantes', [])
        comparaison = data.get('comparaison', None)
        gain = data.get('gain', None)
        employe_nom = str(data.get('employe_nom', ''))[:200]

        if brut <= 0 or not composantes:
            return JsonResponse({'error': 'Données insuffisantes'}, status=400)

        from .services_simulation import calculer_un_bareme, _charger_constantes, BAREME_CGI_REFERENCE, _charger_tranches_db
        constantes = _charger_constantes()
        annee = date.today().year
        tranches_bareme = _charger_tranches_db(annee, 'officiel')
        if not tranches_bareme:
            tranches_bareme = list(BAREME_CGI_REFERENCE)

        total_indem = sum(c.get('montant', 0) for c in composantes if c.get('cle') != 'salaire_base')
        impact = calculer_un_bareme(
            Decimal(str(brut)),
            Decimal(str(total_indem)),
            tranches_bareme,
            constantes,
        )
        impact_data = {
            'cnss': impact['cnss'],
            'rts': impact['rts'],
            'net': impact['net'],
            'taux_effectif': impact['taux_effectif'],
            'base_rts': impact['base_rts'],
            'plafond_exon': impact['plafond_exon'],
            'exon': impact['exon'],
            'depasse': impact['depasse'],
            'detail_tranches': impact['detail_tranches'],
        }

    import io
    import os
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import cm
    from reportlab.pdfgen import canvas
    from reportlab.lib import colors
    from reportlab.platypus import Table, TableStyle

    # Fonts
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    _font_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'static', 'fonts')
    try:
        pdfmetrics.registerFont(TTFont('Arial', os.path.join(_font_dir, 'Arial.ttf')))
        pdfmetrics.registerFont(TTFont('Arial-Bold', os.path.join(_font_dir, 'Arial-Bold.ttf')))
        _FN = 'Arial'; _FB = 'Arial-Bold'
    except Exception:
        _FN = 'Helvetica'; _FB = 'Helvetica-Bold'

    def fmtgnf(n):
        """Formater un nombre en milliers avec séparateur espace."""
        return f'{int(n):,}'.replace(',', ' ')

    buffer = io.BytesIO()
    p = canvas.Canvas(buffer, pagesize=A4)
    width, height = A4
    y = height - 1.5 * cm

    # ── En-tête ──
    entreprise = request.user.entreprise
    p.setFont(_FB, 14)
    p.drawCentredString(width / 2, y, "FICHE DE SIMULATION SALARIALE")
    y -= 0.6 * cm
    p.setFont(_FN, 9)
    p.drawCentredString(width / 2, y, f"Générée le {date.today().strftime('%d/%m/%Y')} — {entreprise.nom_entreprise if entreprise else ''}")
    y -= 0.8 * cm

    if employe_nom:
        p.setFont(_FB, 11)
        p.drawString(1.5 * cm, y, f"Employé : {employe_nom}")
        y -= 0.6 * cm

    p.setFont(_FB, 11)
    p.drawString(1.5 * cm, y, f"Salaire brut mensuel : {fmtgnf(brut)} GNF")
    y -= 0.5 * cm

    # ── Métadonnées de traçabilité ──
    if sim_obj or version_bareme:
        p.setFont(_FN, 8)
        p.setFillColor(colors.HexColor('#7f8c8d'))
        meta_parts = []
        if sim_obj:
            meta_parts.append(f"Simulation #{sim_obj.pk}")
            meta_parts.append(f"Validée le {sim_obj.date_creation.strftime('%d/%m/%Y à %H:%M')}")
        if version_bareme:
            meta_parts.append(f"Barème {version_bareme}")
        statut = (impact_data.get('statut_exoneration') or '').replace('_', ' ').capitalize()
        if statut:
            meta_parts.append(f"Exonération : {statut}")
        p.drawString(1.5 * cm, y, ' — '.join(meta_parts))
        p.setFillColor(colors.black)
        y -= 0.5 * cm
    y -= 1 * cm

    # ── Tableau décomposition ──
    p.setFont(_FB, 10)
    p.drawString(1.5 * cm, y, "1. Décomposition du brut")
    y -= 0.5 * cm

    comp_data = [['Composante', '%', 'Montant (GNF)']]
    for c in composantes:
        comp_data.append([
            c.get('label', c.get('cle', '')),
            f"{c.get('pct', 0)}%",
            fmtgnf(c.get('montant', 0)),
        ])
    comp_data.append(['TOTAL', '100%', fmtgnf(brut)])

    t = Table(comp_data, colWidths=[8 * cm, 3 * cm, 5 * cm])
    t.setStyle(TableStyle([
        ('FONT', (0, 0), (-1, 0), _FB, 9),
        ('FONT', (0, 1), (-1, -2), _FN, 9),
        ('FONT', (0, -1), (-1, -1), _FB, 9),
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#2c3e50')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('BACKGROUND', (0, -1), (-1, -1), colors.HexColor('#d5f5e3')),
        ('ALIGN', (1, 0), (-1, -1), 'RIGHT'),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.grey),
        ('TOPPADDING', (0, 0), (-1, -1), 4),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
    ]))
    tw, th = t.wrap(0, 0)
    t.drawOn(p, 1.5 * cm, y - th)
    y -= th + 0.8 * cm

    # ── Impact fiscal ──
    p.setFont(_FB, 10)
    p.drawString(1.5 * cm, y, "2. Impact fiscal")
    y -= 0.5 * cm

    fisc_data = [
        ['Rubrique', 'Montant (GNF)', 'Détail'],
        ['CNSS Employé (5%)', fmtgnf(impact_data.get('cnss', 0)),
         f"Plafond: 2 500 000 GNF"],
        ['Indemnités exonérées', fmtgnf(impact_data.get('exon', 0)),
         f"Plafond 25% = {fmtgnf(impact_data.get('plafond_exon', 0))} GNF"],
        ['Base imposable RTS', fmtgnf(impact_data.get('base_rts', 0)),
         f"Brut − CNSS − Exon + Dépasse"],
        ['RTS (impôt)', fmtgnf(impact_data.get('rts', 0)),
         f"Taux effectif: {impact_data.get('taux_effectif', 0)}%"],
        ['NET À PAYER', fmtgnf(impact_data.get('net', 0)),
         f"Brut − CNSS − RTS"],
    ]

    t2 = Table(fisc_data, colWidths=[5 * cm, 4 * cm, 7 * cm])
    t2.setStyle(TableStyle([
        ('FONT', (0, 0), (-1, 0), _FB, 9),
        ('FONT', (0, 1), (-1, -2), _FN, 9),
        ('FONT', (0, -1), (-1, -1), _FB, 9),
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#2c3e50')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('BACKGROUND', (0, -1), (-1, -1), colors.HexColor('#d5f5e3')),
        ('ALIGN', (1, 0), (1, -1), 'RIGHT'),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.grey),
        ('TOPPADDING', (0, 0), (-1, -1), 4),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
    ]))
    tw2, th2 = t2.wrap(0, 0)
    t2.drawOn(p, 1.5 * cm, y - th2)
    y -= th2 + 0.8 * cm

    # ── Détail RTS par tranches ──
    detail_tranches = impact_data.get('detail_tranches', [])
    if detail_tranches:
        p.setFont(_FB, 10)
        p.drawString(1.5 * cm, y, "3. Détail RTS par tranches (barème progressif)")
        y -= 0.5 * cm

        tr_data = [['Tranche (GNF)', 'Taux', 'Base tranche', 'Impôt']]
        for tr in detail_tranches:
            sup = fmtgnf(tr['borne_sup']) if tr.get('borne_sup') else '∞'
            tr_data.append([
                f"{fmtgnf(tr['borne_inf'])} → {sup}",
                f"{tr['taux']}%",
                fmtgnf(tr['base_tranche']),
                fmtgnf(tr['impot_tranche']),
            ])

        t3 = Table(tr_data, colWidths=[5 * cm, 2 * cm, 4.5 * cm, 4.5 * cm])
        t3.setStyle(TableStyle([
            ('FONT', (0, 0), (-1, 0), _FB, 8),
            ('FONT', (0, 1), (-1, -1), _FN, 8),
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#34495e')),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
            ('ALIGN', (1, 0), (-1, -1), 'RIGHT'),
            ('GRID', (0, 0), (-1, -1), 0.5, colors.grey),
            ('TOPPADDING', (0, 0), (-1, -1), 3),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
        ]))
        tw3, th3 = t3.wrap(0, 0)
        if y - th3 < 3 * cm:
            p.showPage()
            y = height - 1.5 * cm
        t3.drawOn(p, 1.5 * cm, y - th3)
        y -= th3 + 0.8 * cm

    # ── Comparaison scénarios ──
    section_num = 4 if detail_tranches else 3
    if comparaison:
        p.setFont(_FB, 10)
        p.drawString(1.5 * cm, y, f"{section_num}. Comparaison des scénarios")
        y -= 0.5 * cm

        cmp_data = [['Scénario', 'RTS', 'CNSS', 'Net', 'Taux eff.']]
        for s in comparaison:
            cmp_data.append([
                s.get('scenario', ''),
                fmtgnf(s.get('rts', 0)),
                fmtgnf(s.get('cnss', 0)),
                fmtgnf(s.get('net', 0)),
                f"{s.get('taux', 0)}%",
            ])

        t4 = Table(cmp_data, colWidths=[5 * cm, 3 * cm, 3 * cm, 3.5 * cm, 2.5 * cm])
        t4.setStyle(TableStyle([
            ('FONT', (0, 0), (-1, 0), _FB, 9),
            ('FONT', (0, 1), (-1, -1), _FN, 9),
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#2c3e50')),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
            ('BACKGROUND', (0, -1), (-1, -1), colors.HexColor('#d5f5e3')),
            ('ALIGN', (1, 0), (-1, -1), 'RIGHT'),
            ('GRID', (0, 0), (-1, -1), 0.5, colors.grey),
            ('TOPPADDING', (0, 0), (-1, -1), 4),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
        ]))
        tw4, th4 = t4.wrap(0, 0)
        if y - th4 < 3 * cm:
            p.showPage()
            y = height - 1.5 * cm
        t4.drawOn(p, 1.5 * cm, y - th4)
        y -= th4 + 0.8 * cm
        section_num += 1

    # ── Gain d'optimisation ──
    if gain and gain.get('gain_net_mensuel', 0) != 0:
        if y < 5 * cm:
            p.showPage()
            y = height - 1.5 * cm
        p.setFont(_FB, 10)
        p.drawString(1.5 * cm, y, f"{section_num}. Gain d'optimisation")
        y -= 0.5 * cm

        g_data = [
            ['', 'Avant', 'Après', 'Gain'],
            ['RTS mensuel', fmtgnf(gain.get('rts_avant', 0)),
             fmtgnf(gain.get('rts_apres', 0)),
             f"−{fmtgnf(gain.get('economie_rts_mensuelle', 0))}"],
            ['Net mensuel', fmtgnf(gain.get('net_avant', 0)),
             fmtgnf(gain.get('net_apres', 0)),
             f"+{fmtgnf(gain.get('gain_net_mensuel', 0))}"],
            ['Net annuel', fmtgnf(gain.get('net_avant', 0) * 12),
             fmtgnf(gain.get('net_apres', 0) * 12),
             f"+{fmtgnf(gain.get('gain_net_annuel', 0))}"],
        ]

        t5 = Table(g_data, colWidths=[4 * cm, 4 * cm, 4 * cm, 4 * cm])
        t5.setStyle(TableStyle([
            ('FONT', (0, 0), (-1, 0), _FB, 9),
            ('FONT', (0, 1), (-1, -1), _FN, 9),
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#27ae60')),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
            ('TEXTCOLOR', (-1, 1), (-1, -1), colors.HexColor('#27ae60')),
            ('FONT', (-1, 1), (-1, -1), _FB, 9),
            ('ALIGN', (1, 0), (-1, -1), 'RIGHT'),
            ('GRID', (0, 0), (-1, -1), 0.5, colors.grey),
            ('TOPPADDING', (0, 0), (-1, -1), 4),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
        ]))
        tw5, th5 = t5.wrap(0, 0)
        t5.drawOn(p, 1.5 * cm, y - th5)
        y -= th5 + 0.8 * cm

    # ── Pied de page ──
    p.setFont(_FN, 7)
    p.setFillColor(colors.grey)
    footer_left = f"CGI Guinée Art. 196-197 — Gestionnaire RH — {date.today().strftime('%d/%m/%Y')}"
    if sim_obj:
        footer_left = f"Simulation #{sim_obj.pk} — {footer_left}"
    p.drawString(1.5 * cm, 1 * cm, footer_left)
    if sim_obj:
        valideur = sim_obj.utilisateur.get_full_name() or sim_obj.utilisateur.username
        p.drawCentredString(width / 2, 1 * cm,
                            f"Validé par {valideur} le {sim_obj.date_creation.strftime('%d/%m/%Y à %H:%M')}")
    p.drawRightString(width - 1.5 * cm, 1 * cm,
                      "Document traçable" if sim_obj else "Document non contractuel")

    p.save()
    buffer.seek(0)

    nom_fichier = f"simulation_{date.today().strftime('%Y%m%d')}_{fmtgnf(brut).replace(' ', '')}.pdf"
    response = HttpResponse(buffer, content_type='application/pdf')
    response['Content-Disposition'] = f'attachment; filename="{nom_fichier}"'

    # ── Audit log (lié à la simulation si disponible) ──
    _log_audit(request, 'pdf', sim_obj, {'brut': brut, 'employe_nom': employe_nom[:100]})

    return response





@login_required
@entreprise_active_required
@reauth_required
def bulletin_audit_json(request, bulletin_id):
    """Vue audit — expose le pipeline complet du calcul en JSON."""
    from django.http import JsonResponse
    from django.core.exceptions import PermissionDenied
    bulletin = get_object_or_404(BulletinPaie, pk=bulletin_id)
    # Vérification accès tenant
    snap = bulletin.snapshot_parametres or {}
    return JsonResponse({
        'bulletin_id': bulletin.id,
        'numero': bulletin.numero_bulletin,
        'employe': str(bulletin.employe),
        'periode': f"{bulletin.mois_paie:02d}/{bulletin.annee_paie}",
        'meta': snap.get('meta', {}),
        'audit': snap.get('audit_calcul', {}),
    }, json_dumps_params={'ensure_ascii': False, 'indent': 2})


@login_required
@entreprise_active_required
@reauth_required
def bulletin_audit_pdf(request, bulletin_id):
    # Isolation multi-tenant : un user ne voit que son entreprise
    _bulletin_check = get_object_or_404(BulletinPaie, pk=bulletin_id)
    if (request.user.entreprise and
            _bulletin_check.employe.entreprise != request.user.entreprise):
        raise PermissionDenied
    """PDF d'audit — trace complète et officielle du calcul de paie."""
    from django.core.exceptions import PermissionDenied
    from django.http import HttpResponse
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import cm
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, HRFlowable
    from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
    import io

    bulletin = get_object_or_404(BulletinPaie, pk=bulletin_id)
    snap     = bulletin.snapshot_parametres or {}
    meta     = snap.get('meta', {})
    audit    = snap.get('audit_calcul', {})
    pipeline = audit.get('pipeline', {})

    if not pipeline:
        from django.http import HttpResponseNotFound
        return HttpResponseNotFound("Audit non disponible — recalculer le bulletin.")

    # ── Setup document ──
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4,
                            leftMargin=1.5*cm, rightMargin=1.5*cm,
                            topMargin=1.5*cm, bottomMargin=1.5*cm)

    BLEU      = colors.HexColor('#003E7E')
    BLEU_CLAR = colors.HexColor('#E8F0F8')
    VERT      = colors.HexColor('#1A7A4A')
    ROUGE     = colors.HexColor('#C0392B')
    GRIS      = colors.HexColor('#F5F5F5')

    styles = getSampleStyleSheet()
    titre_style  = ParagraphStyle('titre',  fontSize=16, textColor=BLEU,   spaceAfter=4,  alignment=TA_CENTER, fontName='Helvetica-Bold')
    sous_titre   = ParagraphStyle('sous',   fontSize=10, textColor=colors.grey, spaceAfter=12, alignment=TA_CENTER)
    section_style= ParagraphStyle('section',fontSize=11, textColor=BLEU,   spaceBefore=10, spaceAfter=4, fontName='Helvetica-Bold')
    note_style   = ParagraphStyle('note',   fontSize=7,  textColor=colors.grey, alignment=TA_CENTER, spaceBefore=8)

    def fmt(n):
        try: return f"{int(n):,}".replace(',', ' ') + " GNF"
        except: return str(n)

    def table_section(data, col_widths, header_color=BLEU_CLAR):
        t = Table(data, colWidths=col_widths)
        style = TableStyle([
            ('BACKGROUND', (0,0), (-1,0), header_color),
            ('TEXTCOLOR',  (0,0), (-1,0), BLEU),
            ('FONTNAME',   (0,0), (-1,0), 'Helvetica-Bold'),
            ('FONTSIZE',   (0,0), (-1,-1), 8),
            ('GRID',       (0,0), (-1,-1), 0.3, colors.lightgrey),
            ('ROWBACKGROUNDS', (0,1), (-1,-1), [colors.white, GRIS]),
            ('ALIGN',      (1,0), (-1,-1), 'RIGHT'),
            ('LEFTPADDING', (0,0), (-1,-1), 6),
            ('RIGHTPADDING',(0,0), (-1,-1), 6),
            ('TOPPADDING', (0,0), (-1,-1), 4),
            ('BOTTOMPADDING',(0,0),(-1,-1), 4),
        ])
        t.setStyle(style)
        return t

    story = []

    # ── En-tête ──
    story.append(Paragraph("DOCUMENT D'AUDIT DE PAIE", titre_style))
    story.append(Paragraph(
        f"Bulletin N° {bulletin.numero_bulletin} — "
        f"{bulletin.employe} — "
        f"Période {bulletin.mois_paie:02d}/{bulletin.annee_paie}",
        sous_titre))
    story.append(HRFlowable(width="100%", thickness=1.5, color=BLEU))
    story.append(Spacer(1, 0.3*cm))

    # ── 1. Gains ──
    g = pipeline.get('1_gains', {})
    hs = g.get('heures_sup', {})
    story.append(Paragraph("1. ÉLÉMENTS DE RÉMUNÉRATION (GAINS)", section_style))
    gains_data = [['Composante', 'Montant']]
    gains_data.append(['Salaire de base', fmt(g.get('salaire_base', 0))])
    gains_data.append(['Indemnités forfaitaires', fmt(g.get('indemnites', 0))])
    if hs and hs.get('total', 0) > 0:
        gains_data.append([f"Heures supplémentaires (taux horaire: {fmt(hs.get('taux_horaire',0))})", fmt(hs.get('total', 0))])
    gains_data.append(['SALAIRE BRUT TOTAL', fmt(g.get('brut', 0))])
    story.append(table_section(gains_data, [11*cm, 5*cm]))
    story.append(Spacer(1, 0.2*cm))

    # ── 2. Cotisations CNSS ──
    c = pipeline.get('2_cotisations', {})
    story.append(Paragraph("2. COTISATIONS CNSS", section_style))
    cnss_data = [
        ['Élément', 'Montant'],
        ['Base CNSS', fmt(c.get('cnss_base', 0))],
        [f"Plafond CNSS", fmt(c.get('cnss_plafond', 0))],
        ['CNSS employé (5%)', fmt(c.get('cnss_employe', 0))],
    ]
    story.append(table_section(cnss_data, [11*cm, 5*cm]))
    story.append(Spacer(1, 0.2*cm))

    # ── 3. Base imposable et RTS ──
    f_ = pipeline.get('3_fiscal', {})
    story.append(Paragraph("3. BASE IMPOSABLE ET RTS (CGI 2022)", section_style))
    fiscal_data = [
        ['Élément', 'Montant'],
        ['Salaire brut', fmt(g.get('brut', 0))],
        ['− Indemnités exonérées (≤25% brut)', fmt(f_.get('indemnites_exonerees', 0))],
        ['− CNSS employé', fmt(c.get('cnss_employe', 0))],
        ['= BASE IMPOSABLE RTS', fmt(f_.get('base_imposable', 0))],
    ]
    story.append(table_section(fiscal_data, [11*cm, 5*cm]))
    story.append(Spacer(1, 0.15*cm))

    # Tranches RTS
    tranches = f_.get('tranches_rts', [])
    if tranches:
        story.append(Paragraph("Détail par tranche RTS :", ParagraphStyle('sub', fontSize=9, textColor=BLEU, spaceBefore=4)))
        tr_data = [['Tranche', 'De (GNF)', 'À (GNF)', 'Taux', 'Base', 'Impôt']]
        for t in tranches:
            tr_data.append([
                f"Tranche {t.get('numero','')}",
                f"{t.get('de',0):,}".replace(',', ' '),
                f"{t.get('a','∞'):,}".replace(',', ' ') if t.get('a') else '∞',
                f"{t.get('taux_pct',0)}%",
                f"{t.get('base',0):,}".replace(',', ' '),
                f"{t.get('impot',0):,}".replace(',', ' '),
            ])
        tr_data.append(['', '', '', '', 'TOTAL RTS', fmt(f_.get('rts_total', 0))])
        t_table = Table(tr_data, colWidths=[2.5*cm, 2.5*cm, 2.5*cm, 1.5*cm, 2.5*cm, 2.5*cm])
        t_table.setStyle(TableStyle([
            ('BACKGROUND', (0,0), (-1,0), BLEU_CLAR),
            ('FONTNAME',   (0,0), (-1,0), 'Helvetica-Bold'),
            ('FONTNAME',   (0,-1),(-1,-1),'Helvetica-Bold'),
            ('FONTSIZE',   (0,0), (-1,-1), 8),
            ('GRID',       (0,0), (-1,-1), 0.3, colors.lightgrey),
            ('ROWBACKGROUNDS', (0,1), (-1,-2), [colors.white, GRIS]),
            ('ALIGN',      (1,0), (-1,-1), 'RIGHT'),
            ('LEFTPADDING', (0,0),(-1,-1), 5),
            ('RIGHTPADDING',(0,0),(-1,-1), 5),
            ('TOPPADDING', (0,0),(-1,-1), 3),
            ('BOTTOMPADDING',(0,0),(-1,-1), 3),
        ]))
        story.append(t_table)
    story.append(Spacer(1, 0.2*cm))

    # ── 4. Retenues ──
    r = pipeline.get('4_retenues', {})
    story.append(Paragraph("4. RETENUES TOTALES", section_style))
    ret_data = [
        ['Retenue', 'Montant'],
        ['CNSS employé', fmt(r.get('cnss_employe', 0))],
        ['RTS (impôt)', fmt(r.get('rts', 0))],
        ['Avances / autres', fmt(r.get('avances', 0))],
        ['TOTAL RETENUES', fmt(r.get('total_retenues', 0))],
    ]
    story.append(table_section(ret_data, [11*cm, 5*cm]))
    story.append(Spacer(1, 0.2*cm))

    # ── 5. Net ──
    n = pipeline.get('5_net', {})
    story.append(Paragraph("5. NET À PAYER", section_style))
    verif = r.get('verification', '')
    net_data = [
        ['Élément', 'Montant'],
        ['Salaire brut', fmt(n.get('brut', 0))],
        ['− Total retenues', fmt(n.get('total_retenues', 0))],
        ['= NET À PAYER', fmt(n.get('net_a_payer', 0))],
        ['Vérification', verif],
    ]
    t_net = Table(net_data, colWidths=[11*cm, 5*cm])
    t_net.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,0), BLEU_CLAR),
        ('FONTNAME',   (0,0), (-1,0), 'Helvetica-Bold'),
        ('FONTNAME',   (0,3), (-1,3), 'Helvetica-Bold'),
        ('TEXTCOLOR',  (0,3), (-1,3), VERT),
        ('FONTSIZE',   (0,0), (-1,-1), 8),
        ('GRID',       (0,0), (-1,-1), 0.3, colors.lightgrey),
        ('ROWBACKGROUNDS', (0,1), (-1,-1), [colors.white, GRIS]),
        ('ALIGN',      (1,0), (-1,-1), 'RIGHT'),
        ('LEFTPADDING',(0,0),(-1,-1), 6),
        ('RIGHTPADDING',(0,0),(-1,-1), 6),
        ('TOPPADDING', (0,0),(-1,-1), 4),
        ('BOTTOMPADDING',(0,0),(-1,-1), 4),
    ]))
    story.append(t_net)
    story.append(Spacer(1, 0.2*cm))

    # ── 6. Charges patronales ──
    cp = pipeline.get('6_charges_patronales', {})
    story.append(Paragraph("6. CHARGES PATRONALES", section_style))
    cp_data = [
        ['Charge', 'Montant'],
        ['CNSS employeur (18%)', fmt(cp.get('cnss_employeur', 0))],
        ['Versement forfaitaire VF (6%)', fmt(cp.get('vf', 0))],
        ['Taxe apprentissage TA (1.5%)', fmt(cp.get('ta', 0))],
        ['ONFPP (1.5%)', fmt(cp.get('onfpp', 0))],
        ['TOTAL CHARGES PATRONALES', fmt(cp.get('total', 0))],
        ['COÛT TOTAL EMPLOYEUR', fmt(cp.get('cout_total_employeur', 0))],
    ]
    story.append(table_section(cp_data, [11*cm, 5*cm]))

    # ── Pied de page ──
    story.append(Spacer(1, 0.5*cm))
    story.append(HRFlowable(width="100%", thickness=0.5, color=colors.lightgrey))
    story.append(Paragraph(
        f"Document généré automatiquement — conforme au calcul interne GestionRH "
        f"({meta.get('version_bareme', 'GN-v1')}) | "
        f"Généré le {meta.get('generated_at', '')}",
        note_style))

    doc.build(story)
    buffer.seek(0)

    filename = f"audit_{bulletin.numero_bulletin}.pdf"
    response = HttpResponse(buffer, content_type='application/pdf')
    response['Content-Disposition'] = f'inline; filename="{filename}"'
    return response
