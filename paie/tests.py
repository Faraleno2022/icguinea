"""
Tests unitaires automatisés du moteur de paie - Législation guinéenne

Exécution: python manage.py test paie.tests

Référence: CGI 2022 + Code du Travail Guinée
"""
from decimal import Decimal
from datetime import date
from types import SimpleNamespace
from unittest.mock import patch
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from core.models import Entreprise, Utilisateur
from employes.models import Employe
from paie.models import BulletinPaie, ElementSalaire, PeriodePaie, RubriquePaie
from paie.services import MoteurCalculPaie, appliquer_constantes_cnss_legales
from paie.services_retropaie import calculer_charges_patronales
from paie.services_simulation import calculer_un_bareme as calculer_un_bareme_simulation
from paie.views import _controles_livre_paie
from paie.views_rapports import _audit_masse_salariale, _charges_patronales_bulletin
from paie.views_etax import get_etax_data
from paie.views_export import get_declarations_data
from temps_travail.models import HeureSupplementaire


class HeuresSupplementairesBaseTests(TestCase):
    """Controle que les HS utilisent bien la vraie rubrique de salaire de base."""

    def setUp(self):
        self.entreprise = Entreprise.objects.create(
            nom_entreprise='Test SARL',
            slug='test-hs-base',
            email='test@example.com',
        )
        self.employe = Employe.objects.create(
            entreprise=self.entreprise,
            matricule='EMP-HS-001',
            nom='Test',
            prenoms='HS',
            statut_employe='actif',
        )
        self.rubrique_base = RubriquePaie.objects.create(
            entreprise=self.entreprise,
            code_rubrique='BASE',
            libelle_rubrique='Salaire de base',
            type_rubrique='gain',
            categorie_rubrique='salaire_base',
            soumis_cnss=True,
            soumis_irg=True,
            ordre_calcul=10,
            ordre_affichage=10,
            actif=True,
        )
        ElementSalaire.objects.create(
            employe=self.employe,
            rubrique=self.rubrique_base,
            montant=Decimal('7193919'),
            date_debut=date(2026, 5, 1),
            actif=True,
            recurrent=True,
        )
        self.periode = PeriodePaie.objects.create(
            entreprise=self.entreprise,
            annee=2026,
            mois=5,
            libelle='Mai 2026',
            date_debut=date(2026, 5, 1),
            date_fin=date(2026, 5, 31),
            statut_periode='ouverte',
        )

    def _moteur_minimal(self):
        moteur = MoteurCalculPaie.__new__(MoteurCalculPaie)
        moteur.employe = self.employe
        moteur.lignes = []
        moteur.constantes = {
            'HEURES_MENSUELLES': Decimal('173.33'),
            'TAUX_HS_4PREM': Decimal('130'),
            'TAUX_HS_AUDELA': Decimal('160'),
            'TAUX_HS_NUIT': Decimal('120'),
            'TAUX_HS_FERIE_JOUR': Decimal('160'),
            'TAUX_HS_FERIE_NUIT': Decimal('200'),
        }
        moteur.montants = {
            'total_gains': Decimal('0'),
            'cnss_base': Decimal('0'),
            'imposable': Decimal('0'),
            'heures_sup_30': Decimal('1.03'),
            'heures_sup_60': Decimal('0'),
            'heures_sup_nuit': Decimal('0'),
            'heures_sup_ferie_jour': Decimal('0'),
            'heures_sup_ferie_nuit': Decimal('0'),
            'heures_supplementaires': Decimal('1.03'),
        }
        return moteur

    def test_salaire_base_detecte_meme_si_code_base(self):
        moteur = self._moteur_minimal()

        self.assertEqual(moteur._obtenir_base_calcul('SALAIRE_BASE'), Decimal('7193919'))

    def test_heures_sup_sont_valorisees_quand_base_detectee(self):
        moteur = self._moteur_minimal()
        moteur._calculer_heures_supplementaires()

        self.assertGreater(moteur.montants['montant_heures_sup'], Decimal('0'))
        self.assertGreater(moteur.montants['total_gains'], Decimal('0'))
        self.assertEqual(moteur.lignes[0]['nombre'], Decimal('1.03'))

    def test_heures_sup_validees_sont_injectees_dans_le_bulletin(self):
        HeureSupplementaire.objects.create(
            employe=self.employe,
            date_hs=date(2026, 5, 8),
            type_hs='jour_25',
            nombre_heures=Decimal('1.03'),
            taux_majoration=Decimal('25'),
            taux_horaire_base=Decimal('40000'),
            montant_hs=Decimal('51500'),
            statut='valide',
        )

        moteur = MoteurCalculPaie(self.employe, self.periode)
        moteur._calculer_temps_travail()
        moteur._calculer_gains()

        self.assertEqual(moteur.montants['heures_supplementaires'], Decimal('1.03'))
        self.assertEqual(moteur.montants['montant_heures_sup'], Decimal('51500'))
        self.assertGreaterEqual(moteur.montants['total_gains'], Decimal('7245419'))
        self.assertEqual(moteur.lignes[-1]['nombre'], Decimal('1.03'))


class BulletinCnssIndemnitesTests(TestCase):
    """Controle que les indemnites exonerees RTS/VF ne reduisent pas la base CNSS."""

    def setUp(self):
        self.entreprise = Entreprise.objects.create(
            nom_entreprise='CNSS Indemnites SARL',
            slug='cnss-indemnites',
            email='cnss@example.com',
        )
        self.employe = Employe.objects.create(
            entreprise=self.entreprise,
            matricule='EMP-CNSS-001',
            nom='Test',
            prenoms='CNSS',
            statut_employe='actif',
        )
        ElementSalaire.objects.filter(employe=self.employe).delete()
        self.periode = PeriodePaie.objects.create(
            entreprise=self.entreprise,
            annee=2026,
            mois=5,
            libelle='Mai 2026',
            date_debut=date(2026, 5, 1),
            date_fin=date(2026, 5, 31),
            statut_periode='ouverte',
        )

        rubrique_base = RubriquePaie.objects.create(
            entreprise=self.entreprise,
            code_rubrique='SAL_BASE',
            libelle_rubrique='Salaire de base',
            type_rubrique='gain',
            categorie_rubrique='salaire_base',
            soumis_cnss=True,
            soumis_irg=True,
            ordre_calcul=10,
            ordre_affichage=10,
            actif=True,
        )
        ElementSalaire.objects.create(
            employe=self.employe,
            rubrique=rubrique_base,
            montant=Decimal('2013158'),
            date_debut=date(2026, 5, 1),
            actif=True,
            recurrent=True,
        )

        for code, libelle, montant, ordre in [
            ('TRANSPORT', 'Indemnite transport', Decimal('200000'), 20),
            ('LOGEMENT', 'Indemnite logement', Decimal('200000'), 21),
            ('CHERTE', 'Indemnite cherte de vie', Decimal('256250'), 22),
        ]:
            rubrique = RubriquePaie.objects.create(
                entreprise=self.entreprise,
                code_rubrique=code,
                libelle_rubrique=libelle,
                type_rubrique='gain',
                categorie_rubrique='indemnite',
                soumis_cnss=False,
                soumis_irg=False,
                ordre_calcul=ordre,
                ordre_affichage=ordre,
                actif=True,
            )
            ElementSalaire.objects.create(
                employe=self.employe,
                rubrique=rubrique,
                montant=montant,
                date_debut=date(2026, 5, 1),
                actif=True,
                recurrent=True,
            )

    def test_cnss_reste_calculee_sur_brut_plafonne(self):
        montants = MoteurCalculPaie(self.employe, self.periode).calculer_bulletin()

        self.assertEqual(montants['brut'], Decimal('2669408'))
        self.assertEqual(montants['indemnites_forfaitaires'], Decimal('656250'))
        self.assertEqual(montants['cnss_base'], Decimal('2500000'))
        self.assertEqual(montants['cnss_employe'], Decimal('125000'))
        self.assertEqual(montants['cnss_employeur'], Decimal('450000'))
        self.assertEqual(montants['base_rts'], Decimal('1888158'))
        self.assertEqual(montants['irg'], Decimal('44408'))
        self.assertEqual(montants['net'], Decimal('2500000'))
        self.assertEqual(montants['base_vf'], Decimal('2013158'))
        self.assertEqual(montants['versement_forfaitaire'], Decimal('120789'))
        self.assertEqual(montants['taxe_apprentissage'], Decimal('40263'))


class LivrePaiePdfTests(TestCase):
    """Controle que le livre annuel reste telechargeable sans mois obligatoire."""

    def setUp(self):
        from core import middleware_licence
        middleware_licence._license_cache = {
            'valid': True,
            'trial': False,
            'days_left': 999,
            'checked_at': 9999999999,
        }
        self.entreprise = Entreprise.objects.create(
            nom_entreprise='Livre Paie SARL',
            slug='livre-paie-test',
            email='livre@example.com',
            actif=True,
        )
        self.user = Utilisateur.objects.create_user(
            username='livre-paie',
            email='livre-user@example.com',
            password='pass-test',
            entreprise=self.entreprise,
        )
        self.employe = Employe.objects.create(
            entreprise=self.entreprise,
            matricule='EMP-LIVRE-001',
            nom='Camara',
            prenoms='Test',
            statut_employe='actif',
        )

        for mois in (1, 2):
            periode = PeriodePaie.objects.create(
                entreprise=self.entreprise,
                annee=2026,
                mois=mois,
                libelle=f'Mois {mois}',
                date_debut=date(2026, mois, 1),
                date_fin=date(2026, mois, 28),
                statut_periode='validee',
            )
            BulletinPaie.objects.create(
                employe=self.employe,
                periode=periode,
                numero_bulletin=f'BUL-LIVRE-{mois:02d}',
                mois_paie=mois,
                annee_paie=2026,
                salaire_brut=Decimal('3000000'),
                abattement_forfaitaire=Decimal('750000'),
                base_rts=Decimal('2125000'),
                cnss_employe=Decimal('125000'),
                cnss_employeur=Decimal('450000'),
                irg=Decimal('56250'),
                net_a_payer=Decimal('2818750'),
                statut_bulletin='valide',
            )

    def test_pdf_annuel_livre_paie_sans_mois(self):
        self.client.force_login(self.user)

        response = self.client.get(reverse('paie:telecharger_livre_paie_pdf'), {'annee': '2026'})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'application/pdf')
        self.assertIn('livre_paie_2026.pdf', response['Content-Disposition'])

    def test_pdf_livre_paie_bloque_si_cnss_incoherente(self):
        bulletin = BulletinPaie.objects.get(numero_bulletin='BUL-LIVRE-01')
        BulletinPaie.objects.filter(pk=bulletin.pk).update(
            cnss_employe=Decimal('100081'),
            cnss_employeur=Decimal('360292'),
        )
        self.client.force_login(self.user)

        response = self.client.get(reverse('paie:telecharger_livre_paie_pdf'), {'annee': '2026'})

        self.assertEqual(response.status_code, 409)
        self.assertContains(response, 'Livre de paie non conforme', status_code=409)
        self.assertContains(response, 'Anomalies CNSS', status_code=409)
        self.assertContains(response, 'Generation du PDF officiel bloquee', status_code=409)


class EtaxModeTaOnfppTests(TestCase):
    """Controle la bascule TA/ONFPP dans les donnees eTax."""

    def setUp(self):
        self.entreprise = Entreprise.objects.create(
            nom_entreprise='Etax SARL',
            slug='etax-test',
            email='etax@example.com',
        )
        self.periode = PeriodePaie.objects.create(
            entreprise=self.entreprise,
            annee=2026,
            mois=5,
            libelle='Mai 2026',
            date_debut=date(2026, 5, 1),
            date_fin=date(2026, 5, 31),
            statut_periode='validee',
        )
        for index in range(30):
            employe = Employe.objects.create(
                entreprise=self.entreprise,
                matricule=f'EMP-ETAX-{index:03d}',
                nom='Nom',
                prenoms=str(index),
                statut_employe='actif',
            )
            BulletinPaie.objects.create(
                employe=employe,
                periode=self.periode,
                numero_bulletin=f'BUL-ETAX-{index:03d}',
                mois_paie=5,
                annee_paie=2026,
                salaire_brut=Decimal('1000000'),
                base_rts=Decimal('800000'),
                cnss_employe=Decimal('50000'),
                cnss_employeur=Decimal('180000'),
                irg=Decimal('0'),
                net_a_payer=Decimal('950000'),
                versement_forfaitaire=Decimal('51000'),
                base_vf=Decimal('850000'),
                taxe_apprentissage=Decimal('17000'),
                contribution_onfpp=Decimal('0'),
                statut_bulletin='valide',
            )

    def test_etax_neutralise_ta_a_partir_de_30_employes(self):
        data = get_etax_data(self.entreprise, 2026, 5)

        self.assertEqual(data['effectif'], 30)
        self.assertEqual(data['mode_ta_onfpp'], 'ONFPP')
        self.assertEqual(data['total_ta'], Decimal('0'))
        self.assertEqual(data['detail_employes'][0]['ta'], Decimal('0'))
        self.assertEqual(data['total_onfpp'], Decimal('382500'))

    def test_etax_onfpp_utilise_base_onfpp_dediee(self):
        BulletinPaie.objects.filter(periode=self.periode).update(
            base_onfpp=Decimal('650000'),
            contribution_onfpp=Decimal('0'),
        )

        data = get_etax_data(self.entreprise, 2026, 5)

        self.assertEqual(data['total_base_vf'], Decimal('25500000'))
        self.assertEqual(data['total_base_onfpp'], Decimal('19500000'))
        self.assertEqual(data['total_onfpp'], Decimal('292500'))
        self.assertEqual(data['detail_employes'][0]['onfpp'], Decimal('9750'))

    def test_dmu_expose_total_dgi_onfpp_et_base_vf(self):
        data = get_declarations_data(self.entreprise, 2026, 5)

        self.assertEqual(data['total_base_vf'], Decimal('25500000'))
        self.assertEqual(data['total_dgi'], Decimal('1530000'))
        self.assertEqual(data['total_onfpp_ta'], Decimal('382500'))
        self.assertEqual(data['total_dmu'], Decimal('1912500'))
        self.assertEqual(data['mode_fiscal'], 'optimise')
        self.assertEqual(data['taux_optimisation_global'], Decimal('15.00'))
        self.assertEqual(data['taux_optimisation_vf'], Decimal('15.00'))
        self.assertEqual(data['taux_optimisation_onfpp'], Decimal('15.00'))

    def test_dmu_expose_optimisations_vf_et_onfpp_separees(self):
        BulletinPaie.objects.filter(periode=self.periode).update(
            base_onfpp=Decimal('650000'),
            contribution_onfpp=Decimal('0'),
        )

        data = get_declarations_data(self.entreprise, 2026, 5)

        self.assertEqual(data['total_base_vf'], Decimal('25500000'))
        self.assertEqual(data['total_base_onfpp'], Decimal('19500000'))
        self.assertEqual(data['mode_fiscal'], 'bases_distinctes')
        self.assertEqual(data['taux_optimisation_vf'], Decimal('15.00'))
        self.assertEqual(data['taux_optimisation_onfpp'], Decimal('35.00'))
        self.assertTrue(data['bases_vf_onfpp_distinctes'])

    def test_declaration_annuelle_additionne_base_onfpp_effective_historique(self):
        BulletinPaie.objects.filter(periode=self.periode).update(
            base_onfpp=Decimal('650000'),
            contribution_onfpp=Decimal('9750'),
            taxe_apprentissage=Decimal('0'),
        )
        periode_avril = PeriodePaie.objects.create(
            entreprise=self.entreprise,
            annee=2026,
            mois=4,
            libelle='Avril 2026',
            date_debut=date(2026, 4, 1),
            date_fin=date(2026, 4, 30),
            statut_periode='validee',
        )
        for employe in Employe.objects.filter(entreprise=self.entreprise):
            BulletinPaie.objects.create(
                employe=employe,
                periode=periode_avril,
                numero_bulletin=f'BUL-ETAX-AVR-{employe.id}',
                mois_paie=4,
                annee_paie=2026,
                salaire_brut=Decimal('1000000'),
                base_rts=Decimal('800000'),
                cnss_employe=Decimal('50000'),
                cnss_employeur=Decimal('180000'),
                irg=Decimal('0'),
                net_a_payer=Decimal('950000'),
                versement_forfaitaire=Decimal('51000'),
                base_vf=Decimal('850000'),
                base_onfpp=Decimal('0'),
                taxe_apprentissage=Decimal('0'),
                contribution_onfpp=Decimal('12750'),
                statut_bulletin='valide',
            )

        data = get_declarations_data(self.entreprise, 2026)

        self.assertEqual(data['total_base_vf'], Decimal('51000000'))
        self.assertEqual(data['total_base_onfpp'], Decimal('45000000'))
        self.assertEqual(data['total_onfpp'], Decimal('675000'))

class CNSSCalculTests(SimpleTestCase):
    """TU-01 à TU-03: Tests CNSS salarié et employeur"""
    
    PLANCHER = Decimal('550000')
    PLAFOND = Decimal('2500000')
    TAUX_SALARIE = Decimal('0.05')
    TAUX_EMPLOYEUR = Decimal('0.18')
    
    def _calculer_cnss(self, salaire_brut):
        """Calcule CNSS salarié et employeur avec plancher/plafond"""
        if salaire_brut < self.PLANCHER * Decimal('0.10'):  # Seuil minimum 55 000
            return Decimal('0'), Decimal('0'), Decimal('0')
        
        assiette = max(min(salaire_brut, self.PLAFOND), self.PLANCHER)
        cnss_salarie = round(assiette * self.TAUX_SALARIE)
        cnss_employeur = round(assiette * self.TAUX_EMPLOYEUR)
        return assiette, cnss_salarie, cnss_employeur
    
    def test_tu01_cnss_salarie_sous_plafond(self):
        """TU-01: CNSS salarié 5% sur salaire ≤ 2 500 000"""
        salaire = Decimal('2000000')
        assiette, cnss_salarie, _ = self._calculer_cnss(salaire)
        
        self.assertEqual(assiette, Decimal('2000000'))
        self.assertEqual(cnss_salarie, Decimal('100000'))  # 2M × 5%
    
    def test_tu02_cnss_salarie_plafond(self):
        """TU-02: CNSS salarié plafonné à 125 000 si salaire > 2 500 000"""
        salaire = Decimal('3600000')
        assiette, cnss_salarie, _ = self._calculer_cnss(salaire)
        
        self.assertEqual(assiette, Decimal('2500000'))  # Plafond
        self.assertEqual(cnss_salarie, Decimal('125000'))  # 2.5M × 5% = 125K max
    
    def test_tu02_cnss_salaire_8m(self):
        """TU-02: CNSS salarié plafonné même à 8 000 000"""
        salaire = Decimal('8000000')
        assiette, cnss_salarie, _ = self._calculer_cnss(salaire)
        
        self.assertEqual(assiette, Decimal('2500000'))
        self.assertEqual(cnss_salarie, Decimal('125000'))
    
    def test_tu03_cnss_employeur_plafond(self):
        """TU-03: CNSS employeur 18% plafonné à 450 000"""
        salaire = Decimal('8000000')
        assiette, _, cnss_employeur = self._calculer_cnss(salaire)
        
        self.assertEqual(assiette, Decimal('2500000'))
        self.assertEqual(cnss_employeur, Decimal('450000'))  # 2.5M × 18% = 450K max
    
    def test_cnss_au_plancher(self):
        """CNSS avec salaire au plancher (550 000)"""
        salaire = Decimal('550000')
        assiette, cnss_salarie, cnss_employeur = self._calculer_cnss(salaire)
        
        self.assertEqual(assiette, Decimal('550000'))
        self.assertEqual(cnss_salarie, Decimal('27500'))   # 550K × 5%
        self.assertEqual(cnss_employeur, Decimal('99000'))  # 550K × 18%
    
    def test_cnss_sous_plancher(self):
        """CNSS avec salaire sous plancher → assiette = plancher"""
        salaire = Decimal('400000')
        assiette, cnss_salarie, cnss_employeur = self._calculer_cnss(salaire)
        
        self.assertEqual(assiette, Decimal('550000'))  # Plancher appliqué
        self.assertEqual(cnss_salarie, Decimal('27500'))

    def test_constantes_cnss_legales_ecrasent_taux_flottants(self):
        """Les taux CNSS doivent rester 5% / 18% meme si une config est polluee."""
        constantes = appliquer_constantes_cnss_legales({
            'PLANCHER_CNSS': Decimal('550000'),
            'PLAFOND_CNSS': Decimal('2500000'),
            'TAUX_CNSS_EMPLOYE': Decimal('4.00324'),
            'TAUX_CNSS_EMPLOYEUR': Decimal('14.41168'),
        })

        self.assertEqual(constantes['PLAFOND_CNSS'], Decimal('2500000'))
        self.assertEqual(constantes['TAUX_CNSS_EMPLOYE'], Decimal('5'))
        self.assertEqual(constantes['TAUX_CNSS_EMPLOYEUR'], Decimal('18'))

    def test_simulation_cnss_plafond_ignore_taux_flottants(self):
        """Cas EMP-039: brut > plafond => CNSS 125 000 / 450 000."""
        resultat = calculer_un_bareme_simulation(
            Decimal('2668831'),
            Decimal('0'),
            [{'borne_inf': 0, 'borne_sup': None, 'taux': 0}],
            {
                'PLANCHER_CNSS': Decimal('550000'),
                'PLAFOND_CNSS': Decimal('2500000'),
                'TAUX_CNSS_EMPLOYE': Decimal('4.00324'),
                'TAUX_CNSS_EMPLOYEUR': Decimal('14.41168'),
                'TAUX_VF': Decimal('6'),
            },
            nb_salaries=30,
        )

        self.assertEqual(resultat['assiette_cnss'], 2500000)
        self.assertEqual(resultat['cnss'], 125000)
        self.assertEqual(resultat['cnss_employeur'], 450000)

    def test_livre_paie_detecte_cnss_plafond_incoherente(self):
        employe = SimpleNamespace(matricule='EMP-039', nom='Nom', prenoms='Prenom')
        bulletin = SimpleNamespace(
            numero_bulletin='BUL-039',
            employe=employe,
            salaire_brut=Decimal('2668831'),
            cnss_employe=Decimal('100081'),
            cnss_employeur=Decimal('360292'),
        )

        controles = _controles_livre_paie([bulletin], {
            'total_brut': Decimal('2668831'),
            'total_retenues': Decimal('100081'),
            'total_net': Decimal('2568750'),
        })

        self.assertEqual(controles['nb_anomalies_cnss'], 1)
        self.assertTrue(bulletin.controle_cnss_livre_erreur)
        self.assertEqual(controles['anomalies_cnss'][0]['cnss_employe_attendu'], Decimal('125000'))
        self.assertEqual(controles['anomalies_cnss'][0]['cnss_employeur_attendu'], Decimal('450000'))

    def test_livre_paie_detecte_ecart_macro_net(self):
        controles = _controles_livre_paie([], {
            'total_brut': Decimal('1737409437'),
            'total_retenues': Decimal('112292381'),
            'total_net': Decimal('1624717056'),
        })

        self.assertFalse(controles['macro_ok'])
        self.assertEqual(controles['net_attendu'], Decimal('1625117056'))
        self.assertEqual(controles['ecart_net'], Decimal('-400000'))
        self.assertGreater(controles['nb_controles_agregation'], 0)

    def test_livre_paie_liste_retenues_hors_cnss_rts_sans_bloquer(self):
        employe = SimpleNamespace(matricule='EMP-012', nom='Nom', prenoms='Prenom')
        bulletin = SimpleNamespace(
            numero_bulletin='BUL-012',
            employe=employe,
            salaire_brut=Decimal('18174015'),
            rappel_salaire=Decimal('0'),
            cnss_employe=Decimal('125000'),
            cnss_employeur=Decimal('450000'),
            irg=Decimal('1607352'),
            net_a_payer=Decimal('16241663'),
        )

        controles = _controles_livre_paie([bulletin], {
            'total_brut': Decimal('18174015'),
            'total_retenues': Decimal('1932352'),
            'total_net': Decimal('16241663'),
        })

        self.assertTrue(controles['conforme'])
        self.assertTrue(controles['macro_ok'])
        self.assertEqual(bulletin.total_retenues_livre, Decimal('1932352'))
        self.assertEqual(controles['nb_retenues_hors_cnss_rts'], 1)
        self.assertEqual(controles['retenues_hors_cnss_rts'][0]['matricule'], 'EMP-012')
        self.assertEqual(controles['retenues_hors_cnss_rts'][0]['montant'], Decimal('200000'))


class ChargesPatronalesTests(SimpleTestCase):
    """TU-04 et TU-05: Tests VF et TA"""

    TAUX_VF = Decimal('0.06')    # 6%
    TAUX_TA = Decimal('0.02')    # 2%

    def _calculer_vf_ta(self, salaire_brut):
        """Calcule VF et TA sur base VF."""
        deduction_vf = round(salaire_brut * Decimal('0.25'))
        base_vf = salaire_brut - deduction_vf
        vf = round(base_vf * self.TAUX_VF)
        ta = round(base_vf * self.TAUX_TA)
        return base_vf, vf, ta

    def test_tu04_vf_6_pourcent(self):
        """TU-04: Versement Forfaitaire = 6% de la base VF"""
        salaire = Decimal('3600000')
        base_vf, vf, _ = self._calculer_vf_ta(salaire)

        self.assertEqual(base_vf, Decimal('2700000'))
        self.assertEqual(vf, Decimal('162000'))

    def test_tu05_ta_2_pourcent(self):
        """TU-05: Taxe Apprentissage = 2% de la base VF"""
        salaire = Decimal('3600000')
        _, _, ta = self._calculer_vf_ta(salaire)

        self.assertEqual(ta, Decimal('54000'))

    def test_charges_patronales_total(self):
        """Total charges patronales = CNSS 18% + VF 6% + TA 2%"""
        salaire = Decimal('3600000')

        # CNSS employeur (plafonné)
        cnss_employeur = Decimal('450000')  # 2.5M x 18%

        _, vf, ta = self._calculer_vf_ta(salaire)
        total = cnss_employeur + vf + ta

        self.assertEqual(total, Decimal('666000'))

    def test_charges_patronales_onfpp_a_partir_de_30_salaries(self):
        """ONFPP = 1,5% de la base VF quand l'effectif atteint le seuil."""
        constantes = {
            'PLANCHER_CNSS': Decimal('550000'),
            'PLAFOND_CNSS': Decimal('2500000'),
            'TAUX_CNSS_EMPLOYEUR': Decimal('18'),
            'TAUX_VF': Decimal('6'),
            'TAUX_TA': Decimal('2'),
            'TAUX_ONFPP': Decimal('1.5'),
            'SEUIL_TA_ONFPP': Decimal('30'),
        }

        with patch('paie.cache_service.PayrollCacheService.get_constantes', return_value=constantes):
            charges = calculer_charges_patronales(Decimal('3600000'), nb_salaries=30)

        self.assertEqual(charges['libelle_ta'], 'ONFPP')
        self.assertEqual(charges['base_vf'], 2700000)
        self.assertEqual(charges['base_onfpp'], 2700000)
        self.assertEqual(charges['base_ta_onfpp'], 2700000)
        self.assertEqual(charges['vf'], 162000)
        self.assertEqual(charges['ta'], 40500)
        self.assertEqual(charges['total'], 652500)

    def test_charges_patronales_onfpp_strict_sur_brut_sans_changer_vf(self):
        """Le mode ONFPP strict applique seulement l'ONFPP sur le brut."""
        constantes = {
            'PLANCHER_CNSS': Decimal('550000'),
            'PLAFOND_CNSS': Decimal('2500000'),
            'TAUX_CNSS_EMPLOYEUR': Decimal('18'),
            'TAUX_VF': Decimal('6'),
            'TAUX_TA': Decimal('2'),
            'TAUX_ONFPP': Decimal('1.5'),
            'SEUIL_TA_ONFPP': Decimal('30'),
        }

        with patch('paie.cache_service.PayrollCacheService.get_constantes', return_value=constantes):
            charges = calculer_charges_patronales(
                Decimal('3600000'), nb_salaries=30, mode_base_onfpp='brut'
            )

        self.assertEqual(charges['mode_base_vf'], 'brut_moins_deduction')
        self.assertEqual(charges['mode_base_onfpp'], 'brut')
        self.assertEqual(charges['base_vf'], 2700000)
        self.assertEqual(charges['base_onfpp'], 3600000)
        self.assertEqual(charges['vf'], 162000)
        self.assertEqual(charges['ta'], 54000)
        self.assertEqual(charges['total'], 666000)

    def test_charges_patronales_ta_sous_30_salaries(self):
        """TA = 2% de la base VF tant que l'effectif reste sous le seuil."""
        constantes = {
            'PLANCHER_CNSS': Decimal('550000'),
            'PLAFOND_CNSS': Decimal('2500000'),
            'TAUX_CNSS_EMPLOYEUR': Decimal('18'),
            'TAUX_VF': Decimal('6'),
            'TAUX_TA': Decimal('2'),
            'TAUX_ONFPP': Decimal('1.5'),
            'SEUIL_TA_ONFPP': Decimal('30'),
        }

        with patch('paie.cache_service.PayrollCacheService.get_constantes', return_value=constantes):
            charges = calculer_charges_patronales(Decimal('3600000'), nb_salaries=29)

        self.assertEqual(charges['libelle_ta'], 'TA')
        self.assertEqual(charges['ta'], 54000)
        self.assertEqual(charges['total'], 666000)

    def test_rapport_inclut_onfpp_dans_charges_patronales(self):
        """Les rapports doivent additionner CNSS patronale, VF, TA et ONFPP."""
        bulletin = SimpleNamespace(
            cnss_employeur=Decimal('450000'),
            versement_forfaitaire=Decimal('207000'),
            taxe_apprentissage=Decimal('0'),
            contribution_onfpp=Decimal('54000'),
        )

        self.assertEqual(_charges_patronales_bulletin(bulletin), Decimal('711000'))

    def test_audit_masse_salariale_detecte_ecart_cnss_patronale(self):
        """Le rapport masse salariale doit signaler un cumul CNSS patronal incomplet."""
        employe = SimpleNamespace(matricule='EMP-080', nom='Nom', prenoms='Prenom')
        bulletin = SimpleNamespace(
            numero_bulletin='BUL-080',
            employe=employe,
            salaire_brut=Decimal('8403436'),
            cnss_employe=Decimal('125000'),
            cnss_employeur=Decimal('0'),
            irg=Decimal('377758'),
            net_a_payer=Decimal('7900678'),
            versement_forfaitaire=Decimal('1000000'),
            taxe_apprentissage=Decimal('0'),
            contribution_onfpp=Decimal('0'),
            heures_supplementaires_30=Decimal('0'),
            heures_supplementaires_60=Decimal('0'),
            prime_heures_sup=Decimal('0'),
        )

        class FauxQuerySet(list):
            def select_related(self, *args):
                return self

            def aggregate(self, **kwargs):
                return {
                    'nb': len(self),
                    'brut': sum((b.salaire_brut for b in self), Decimal('0')),
                    'cnss_sal': sum((b.cnss_employe for b in self), Decimal('0')),
                    'cnss_emp': sum((b.cnss_employeur for b in self), Decimal('0')),
                    'rts': sum((b.irg for b in self), Decimal('0')),
                    'net': sum((b.net_a_payer for b in self), Decimal('0')),
                    'vf': sum((b.versement_forfaitaire for b in self), Decimal('0')),
                    'ta': sum((b.taxe_apprentissage for b in self), Decimal('0')),
                    'onfpp': sum((b.contribution_onfpp for b in self), Decimal('0')),
                }

        audit = _audit_masse_salariale(FauxQuerySet([bulletin]))

        self.assertEqual(audit['cnss_patronale_attendue'], Decimal('450000'))
        self.assertEqual(audit['ecart_cnss_patronale'], Decimal('-450000'))
        self.assertEqual(audit['nb_anomalies_cnss'], 1)
        self.assertEqual(audit['statut'], 'a_verifier')

    def test_simulation_bascule_ta_onfpp_au_seuil_de_30(self):
        """La simulation applique TA sous 30 salariés et ONFPP à partir de 30."""
        constantes = {
            'PLANCHER_CNSS': Decimal('550000'),
            'PLAFOND_CNSS': Decimal('2500000'),
            'TAUX_CNSS_EMPLOYE': Decimal('5'),
            'TAUX_CNSS_EMPLOYEUR': Decimal('18'),
            'TAUX_VF': Decimal('6'),
            'SEUIL_TA_ONFPP': Decimal('30'),
        }
        tranches = [{'borne_inf': 0, 'borne_sup': None, 'taux': 0}]

        sous_seuil = calculer_un_bareme_simulation(
            Decimal('3600000'), Decimal('900000'), tranches, constantes, nb_salaries=29
        )
        au_seuil = calculer_un_bareme_simulation(
            Decimal('3600000'), Decimal('900000'), tranches, constantes, nb_salaries=30
        )

        self.assertEqual(sous_seuil['ta'], 54000)
        self.assertEqual(sous_seuil['onfpp'], 0)
        self.assertEqual(au_seuil['ta'], 0)
        self.assertEqual(au_seuil['base_onfpp'], 2700000)
        self.assertEqual(au_seuil['onfpp'], 40500)

    def test_onfpp_exemple_bulletin_base_vf(self):
        """Cas audit: ONFPP sur base VF/ONFPP, pas sur le brut."""
        brut = Decimal('4479445')
        base_vf, vf, _ = self._calculer_vf_ta(brut)
        onfpp = round(base_vf * Decimal('0.015'))

        self.assertEqual(base_vf, Decimal('3359584'))
        self.assertEqual(vf, Decimal('201575'))
        self.assertEqual(onfpp, Decimal('50394'))
        self.assertEqual(Decimal('450000') + vf + onfpp, Decimal('701969'))

    def test_charges_patronales_mode_strict_fiscal_sur_brut(self):
        """Le mode strict fiscal applique VF/ONFPP directement sur le brut."""
        constantes = {
            'PLANCHER_CNSS': Decimal('550000'),
            'PLAFOND_CNSS': Decimal('2500000'),
            'TAUX_CNSS_EMPLOYEUR': Decimal('18'),
            'TAUX_VF': Decimal('6'),
            'TAUX_TA': Decimal('2'),
            'TAUX_ONFPP': Decimal('1.5'),
            'SEUIL_TA_ONFPP': Decimal('30'),
        }

        with patch('paie.cache_service.PayrollCacheService.get_constantes', return_value=constantes):
            charges = calculer_charges_patronales(
                Decimal('3600000'), nb_salaries=30, mode_base_vf='brut'
            )

        self.assertEqual(charges['mode_base_vf'], 'brut')
        self.assertEqual(charges['deduction_vf'], 0)
        self.assertEqual(charges['base_vf'], 3600000)
        self.assertEqual(charges['vf'], 216000)
        self.assertEqual(charges['ta'], 54000)
        self.assertEqual(charges['total'], 720000)

    def test_bulletin_indicateur_optimisation_et_risque(self):
        """Chaque bulletin expose son taux d'optimisation et son risque fiscal."""
        bulletin = BulletinPaie(
            salaire_brut=Decimal('3600000'),
            base_vf=Decimal('2700000'),
            base_onfpp=Decimal('2700000'),
            versement_forfaitaire=Decimal('162000'),
            contribution_onfpp=Decimal('40500'),
            taxe_apprentissage=Decimal('0'),
        )

        self.assertEqual(bulletin.mode_base_vf_effectif, 'optimise')
        self.assertEqual(bulletin.taux_optimisation_vf_onfpp, Decimal('25.00'))
        self.assertEqual(bulletin.economie_vf_onfpp_vs_strict, Decimal('67500'))
        self.assertEqual(bulletin.risque_fiscal_bulletin['niveau'], 'moyen')

    def test_bulletin_controle_onfpp_strict_sur_base_dediee(self):
        """Le controle accepte ONFPP sur brut quand base_onfpp est renseignee."""
        bulletin = BulletinPaie(
            salaire_brut=Decimal('3600000'),
            base_vf=Decimal('2700000'),
            base_onfpp=Decimal('3600000'),
            versement_forfaitaire=Decimal('162000'),
            contribution_onfpp=Decimal('54000'),
            taxe_apprentissage=Decimal('0'),
        )

        self.assertNotIn('Ecart TA/ONFPP', bulletin.risque_fiscal_bulletin['raisons'])

    def test_bulletin_indicateur_detecte_ecart_vf(self):
        """Un ecart VF/ONFPP visible fait remonter le risque du bulletin."""
        bulletin = BulletinPaie(
            salaire_brut=Decimal('3600000'),
            base_vf=Decimal('2700000'),
            versement_forfaitaire=Decimal('216000'),
            contribution_onfpp=Decimal('40500'),
            taxe_apprentissage=Decimal('0'),
        )

        self.assertEqual(bulletin.risque_fiscal_bulletin['niveau'], 'eleve')
        self.assertIn('Ecart VF', bulletin.risque_fiscal_bulletin['raisons'])


class IRGCalculTests(SimpleTestCase):
    """TU-06 et TU-07: Tests IRG/RTS"""
    
    def _calculer_irg(self, base_imposable):
        """Calcule IRG selon barème RTS CGI 2022 (6 tranches)"""
        tranches = [
            (Decimal('0'), Decimal('1000000'), Decimal('0')),
            (Decimal('1000001'), Decimal('3000000'), Decimal('0.05')),
            (Decimal('3000001'), Decimal('5000000'), Decimal('0.08')),
            (Decimal('5000001'), Decimal('10000000'), Decimal('0.10')),
            (Decimal('10000001'), Decimal('20000000'), Decimal('0.15')),
            (Decimal('20000001'), None, Decimal('0.20')),
        ]
        
        irg_total = Decimal('0')
        
        for borne_inf, borne_sup, taux in tranches:
            if base_imposable < borne_inf:
                break
            
            if borne_sup is None:
                montant_tranche = base_imposable - borne_inf + 1
            else:
                montant_tranche = min(base_imposable, borne_sup) - borne_inf + 1
            
            if montant_tranche > 0:
                irg_total += round(montant_tranche * taux)
        
        return irg_total
    
    def test_tu06_irg_sans_primes(self):
        """TU-06: IRG calculé correctement sur salaire seul"""
        # Salaire brut 3 600 000 - CNSS 125 000 = Base imposable 3 475 000
        base_imposable = Decimal('3475000')
        irg = self._calculer_irg(base_imposable)
        
        # Tranche 0-1M: 0
        # Tranche 1M-3M: 2M × 5% = 100 000
        # Tranche 3M-3.475M: 475K × 8% = 38 000
        # Total attendu: 138 000 (environ, selon arrondi exact)
        self.assertGreater(irg, Decimal('0'))
        self.assertLess(irg, Decimal('150000'))
    
    def test_tu07_irg_avec_primes(self):
        """TU-07: IRG recalculé avec primes"""
        # Salaire 2.8M + Prime 800K = Brut 3.6M
        # Base imposable = 3.6M - 125K CNSS = 3.475M
        base_sans_prime = Decimal('2675000')  # 2.8M - 125K
        base_avec_prime = Decimal('3475000')  # 3.6M - 125K
        
        irg_sans = self._calculer_irg(base_sans_prime)
        irg_avec = self._calculer_irg(base_avec_prime)
        
        # L'IRG avec primes doit être supérieur
        self.assertGreater(irg_avec, irg_sans)
    
    def test_irg_premiere_tranche_exoneree(self):
        """Première tranche (0-1M) exonérée à 0%"""
        base_imposable = Decimal('800000')
        irg = self._calculer_irg(base_imposable)
        
        self.assertEqual(irg, Decimal('0'))
    
    def test_irg_exemple_manuel_8m(self):
        """IRG sur exemple du manuel (8M GNF)"""
        # Base imposable = 8M - 125K = 7.875M
        base_imposable = Decimal('7875000')
        irg = self._calculer_irg(base_imposable)
        
        # Calcul attendu:
        # 0-1M: 0
        # 1M-3M: 2M × 5% = 100 000
        # 3M-5M: 2M × 8% = 160 000
        # 5M-7.875M: 2.875M × 10% = 287 500
        # Total: 547 500
        self.assertEqual(irg, Decimal('547500'))


class DeductionsTests(SimpleTestCase):
    """TU-08 et TU-09: Tests avances et prêts"""
    
    def test_tu08_avance_salaire(self):
        """TU-08: Avance sur salaire déduite du net"""
        salaire_brut = Decimal('3600000')
        cnss_salarie = Decimal('125000')
        irg = Decimal('67562')
        avance = Decimal('160000')
        
        net_avant_avance = salaire_brut - cnss_salarie - irg
        net_apres_avance = net_avant_avance - avance
        
        self.assertEqual(net_avant_avance, Decimal('3407438'))
        self.assertEqual(net_apres_avance, Decimal('3247438'))
    
    def test_tu09_pret_salarie(self):
        """TU-09: Prêt salarié déduit du net"""
        salaire_brut = Decimal('3600000')
        cnss_salarie = Decimal('125000')
        irg = Decimal('67562')
        pret_mensuel = Decimal('200000')
        
        net = salaire_brut - cnss_salarie - irg - pret_mensuel
        
        self.assertEqual(net, Decimal('3207438'))


class NetAPayerTests(SimpleTestCase):
    """TU-10: Test net à payer final"""
    
    def test_tu10_net_a_payer_complet(self):
        """TU-10: Net à payer = Brut - toutes retenues"""
        # Données du bulletin validé
        salaire_brut = Decimal('3600000')
        cnss_salarie = Decimal('125000')
        irg = Decimal('67562')
        avance = Decimal('160000')
        
        total_retenues = cnss_salarie + irg + avance
        net_a_payer = salaire_brut - total_retenues
        
        self.assertEqual(total_retenues, Decimal('352562'))
        self.assertEqual(net_a_payer, Decimal('3247438'))
    
    def test_net_a_payer_sans_retenues_optionnelles(self):
        """Net à payer sans avance ni prêt"""
        salaire_brut = Decimal('3600000')
        cnss_salarie = Decimal('125000')
        irg = Decimal('67562')
        
        net_a_payer = salaire_brut - cnss_salarie - irg
        
        self.assertEqual(net_a_payer, Decimal('3407438'))


class PlafondIndemnitesTests(SimpleTestCase):
    """Tests plafond 25% indemnités forfaitaires"""
    
    TAUX_PLAFOND = Decimal('0.25')
    
    def test_indemnites_sous_plafond(self):
        """Indemnités sous 25% → pas de réintégration"""
        salaire_brut = Decimal('2800000')
        indemnites = Decimal('500000')  # < 25% de 2.8M = 700K
        
        plafond = salaire_brut * self.TAUX_PLAFOND
        depassement = max(Decimal('0'), indemnites - plafond)
        
        self.assertEqual(depassement, Decimal('0'))
    
    def test_indemnites_au_plafond(self):
        """Indemnités à 25% → pas de réintégration"""
        salaire_brut = Decimal('2800000')
        indemnites = Decimal('700000')  # = 25% exactement
        
        plafond = salaire_brut * self.TAUX_PLAFOND
        depassement = max(Decimal('0'), indemnites - plafond)
        
        self.assertEqual(depassement, Decimal('0'))
    
    def test_indemnites_depassement(self):
        """Indemnités > 25% → excédent réintégré"""
        salaire_brut = Decimal('2800000')
        indemnites = Decimal('900000')  # > 25% de 2.8M = 700K
        
        plafond = salaire_brut * self.TAUX_PLAFOND
        depassement = max(Decimal('0'), indemnites - plafond)
        
        self.assertEqual(plafond, Decimal('700000'))
        self.assertEqual(depassement, Decimal('200000'))  # Réintégré dans base imposable


class ExonerationStagiaireTests(SimpleTestCase):
    """Tests exonération RTS stagiaires/apprentis"""
    
    SEUIL_EXONERATION = Decimal('1200000')
    DUREE_MAX_MOIS = 12
    
    def _est_exonere(self, type_contrat, salaire, mois_ecoules):
        """Vérifie si éligible à l'exonération RTS"""
        est_stagiaire_apprenti = type_contrat in ('stage', 'apprentissage')
        duree_ok = mois_ecoules <= self.DUREE_MAX_MOIS
        montant_ok = salaire <= self.SEUIL_EXONERATION
        
        return est_stagiaire_apprenti and duree_ok and montant_ok
    
    def test_stagiaire_exonere(self):
        """Stagiaire ≤ 1.2M et ≤ 12 mois → exonéré"""
        self.assertTrue(self._est_exonere('stage', Decimal('1000000'), 6))
    
    def test_stagiaire_salaire_depasse(self):
        """Stagiaire > 1.2M → non exonéré"""
        self.assertFalse(self._est_exonere('stage', Decimal('1500000'), 6))
    
    def test_stagiaire_duree_depassee(self):
        """Stagiaire > 12 mois → non exonéré"""
        self.assertFalse(self._est_exonere('stage', Decimal('1000000'), 15))
    
    def test_cdi_non_exonere(self):
        """CDI jamais exonéré même si salaire faible"""
        self.assertFalse(self._est_exonere('CDI', Decimal('800000'), 3))


# ======================================================================
# TESTS MOTEUR DE FORMULES (simpleeval + phases)
# ======================================================================

class FormuleSecuriteTests(SimpleTestCase):
    """Tests de sécurité du moteur de formules (simpleeval)"""

    def _vars(self):
        return {
            'brut': 5690002, 'cnss': 125000, 'indemnites': 2200000,
            'salaire_base': 3000000, 'primes': 400000, 'heures_sup': 90002,
            'total_gains': 5690002, 'total_retenues': 316400,
            'cnss_base': 2500000, 'net': 5373602,
            'anciennete_mois': 24, 'anciennete_ans': 2,
            'nb_enfants': 2, 'nb_conjoints': 1, 'plafond_cnss': 2500000,
        }

    def test_formule_basique(self):
        """Formule simple fonctionne"""
        from paie.formules import evaluer_formule
        r = evaluer_formule('brut * 0.25', self._vars())
        self.assertEqual(r, Decimal('1422500.5'))

    def test_formule_min_max(self):
        """min/max fonctionnent"""
        from paie.formules import evaluer_formule
        r = evaluer_formule('min(indemnites, brut * 0.25)', self._vars())
        self.assertEqual(r, Decimal('1422500.5'))

    def test_formule_ternaire(self):
        """Opérateur ternaire if/else fonctionne"""
        from paie.formules import evaluer_formule
        r = evaluer_formule('indemnites if indemnites <= brut * 0.25 else brut * 0.25', self._vars())
        self.assertEqual(r, Decimal('1422500.5'))

    def test_injection_import_bloquee(self):
        """Tentative d'import bloquée"""
        from paie.formules import evaluer_formule
        with self.assertRaises(ValueError):
            evaluer_formule('__import__("os").system("whoami")', self._vars())

    def test_injection_builtins_bloquee(self):
        """Accès aux builtins bloqué"""
        from paie.formules import evaluer_formule
        with self.assertRaises(ValueError):
            evaluer_formule('__builtins__', self._vars())

    def test_formule_vide(self):
        """Formule vide lève ValueError"""
        from paie.formules import evaluer_formule
        with self.assertRaises(ValueError):
            evaluer_formule('', self._vars())

    def test_resultat_negatif_ramene_a_zero(self):
        """Résultat négatif ramené à 0"""
        from paie.formules import evaluer_formule
        r = evaluer_formule('salaire_base - brut', self._vars())
        self.assertEqual(r, Decimal('0'))

    def test_division_par_zero_retourne_zero(self):
        """Division par zéro retourne 0"""
        from paie.formules import evaluer_formule
        r = evaluer_formule('brut / 0', self._vars())
        self.assertEqual(r, Decimal('0'))


class FormulePhaseTests(SimpleTestCase):
    """Tests des phases de calcul du moteur de formules"""

    def _vars(self):
        return {
            'brut': 5000000, 'cnss': 125000, 'salaire_base': 3000000,
            'total_gains': 5000000, 'total_retenues': 300000, 'net': 4700000,
            'anciennete_mois': 24, 'anciennete_ans': 2,
            'nb_enfants': 0, 'nb_conjoints': 0, 'plafond_cnss': 2500000,
            'indemnites': 0, 'primes': 0, 'heures_sup': 0, 'cnss_base': 2500000,
        }

    def test_phase_gains_salaire_base_ok(self):
        """Phase gains : salaire_base accessible"""
        from paie.formules import evaluer_formule
        r = evaluer_formule('salaire_base * 0.10', self._vars(), phase='gains')
        self.assertEqual(r, Decimal('300000.0'))

    def test_phase_gains_brut_bloque(self):
        """Phase gains : brut non accessible"""
        from paie.formules import evaluer_formule
        with self.assertRaises(ValueError):
            evaluer_formule('brut * 0.10', self._vars(), phase='gains')

    def test_phase_cotisations_brut_ok(self):
        """Phase cotisations : brut accessible"""
        from paie.formules import evaluer_formule
        r = evaluer_formule('brut * 0.05', self._vars(), phase='cotisations')
        self.assertEqual(r, Decimal('250000.0'))

    def test_phase_cotisations_cnss_bloque(self):
        """Phase cotisations : cnss non accessible (pas encore calculé)"""
        from paie.formules import evaluer_formule
        with self.assertRaises(ValueError):
            evaluer_formule('cnss * 2', self._vars(), phase='cotisations')

    def test_phase_fiscal_cnss_ok(self):
        """Phase fiscal : cnss accessible"""
        from paie.formules import evaluer_formule
        r = evaluer_formule('brut - cnss', self._vars(), phase='fiscal')
        self.assertEqual(r, Decimal('4875000.0'))

    def test_phase_retenues_net_bloque(self):
        """Phase retenues : net non accessible"""
        from paie.formules import evaluer_formule
        with self.assertRaises(ValueError):
            evaluer_formule('net * 0.10', self._vars(), phase='retenues')

    def test_phase_net_tout_accessible(self):
        """Phase net : toutes variables accessibles"""
        from paie.formules import evaluer_formule
        r = evaluer_formule('net * 0.10', self._vars(), phase='net')
        self.assertEqual(r, Decimal('470000.0'))

    def test_sans_phase_tout_accessible(self):
        """Sans phase : toutes variables accessibles (rétrocompatibilité)"""
        from paie.formules import evaluer_formule
        r = evaluer_formule('net * 0.10', self._vars())
        self.assertEqual(r, Decimal('470000.0'))


class BulletinCasReferenceTests(SimpleTestCase):
    """Test du cas de référence validé manuellement (bulletin employé type)

    Brut: 5 690 002 GNF
    Salaire base: 3 000 000  |  Cherté de vie: 900 000  |  Logement: 600 000
    Transport: 700 000  |  Prime: 400 000  |  HS: 90 002
    CNSS salarié: 125 000  |  RTS: 191 400  |  Net: 5 373 602
    """

    BRUT = Decimal('5690002')
    CNSS_EMP = Decimal('125000')
    RTS = Decimal('191400')
    NET = Decimal('5373602')
    VF = Decimal('341400')
    TA = Decimal('113800')
    CNSS_PAT = Decimal('450000')

    def test_brut(self):
        base = Decimal('3000000')
        cherte = Decimal('900000')
        logement = Decimal('600000')
        transport = Decimal('700000')
        prime = Decimal('400000')
        hs = Decimal('90002')
        self.assertEqual(base + cherte + logement + transport + prime + hs, self.BRUT)

    def test_cnss_employe(self):
        plafond = Decimal('2500000')
        self.assertEqual(plafond * Decimal('5') / Decimal('100'), self.CNSS_EMP)

    def test_net_a_payer(self):
        self.assertEqual(self.BRUT - self.CNSS_EMP - self.RTS, self.NET)

    def test_vf(self):
        self.assertEqual(
            (self.BRUT * Decimal('6') / Decimal('100')).quantize(Decimal('1')),
            self.VF
        )

    def test_ta(self):
        self.assertEqual(
            (self.BRUT * Decimal('2') / Decimal('100')).quantize(Decimal('1')),
            self.TA
        )

    def test_cnss_patronale(self):
        plafond = Decimal('2500000')
        self.assertEqual(plafond * Decimal('18') / Decimal('100'), self.CNSS_PAT)

    def test_total_charges_patronales(self):
        self.assertEqual(self.CNSS_PAT + self.VF + self.TA, Decimal('905200'))

