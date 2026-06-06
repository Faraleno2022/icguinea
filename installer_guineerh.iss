; GestionnaireRH - Inno Setup Installer Script
; ===============================================
; Auteur  : Guinée RH
; Version : 1.1.0
;
; Prérequis : Inno Setup 6+ (https://jrsoftware.org/isinfo.php)
;
; Pour compiler :
;   1. Installez Inno Setup
;   2. Ouvrez ce fichier dans Inno Setup Compiler
;   3. Appuyez sur Ctrl+F9 (Compile)
;   4. L'installateur est créé dans le dossier "Output"

[Setup]
; ── Identification ─────────────────────────────────────────────────────────────
AppId={{A1B2C3D4-E5F6-7890-ABCD-EF1234567890}
AppName=GestionnaireRH
AppVersion=1.1.0
AppVerName=GestionnaireRH 1.1.0
AppPublisher=Guinée RH
AppPublisherURL=https://www.guineerh.space
AppSupportURL=https://www.guineerh.space
AppCopyright=Copyright © 2024-2026 Guinée RH. Tous droits réservés.

; ── Installation / Mise à jour ─────────────────────────────────────────────────
DefaultDirName={autopf}\GestionnaireRH
DefaultGroupName=GestionnaireRH
AllowNoIcons=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

; ── Mise à jour automatique : pas besoin de désinstaller ───────────────────────
UsePreviousAppDir=yes
CloseApplications=force
RestartApplications=no

; ── Sortie ─────────────────────────────────────────────────────────────────────
OutputDir=Output
OutputBaseFilename=GestionnaireRH_Setup_v1.1.0

; ── Icône ───────────────────────────────────────────────────────────────────────
SetupIconFile=static\img\logo.ico

; ── Compression ────────────────────────────────────────────────────────────────
Compression=lzma2/ultra64
SolidCompression=yes
LZMAUseSeparateProcess=yes

; ── Interface ──────────────────────────────────────────────────────────────────
WizardStyle=modern
WizardSizePercent=100
DisableWelcomePage=no

; ── Désinstallation ────────────────────────────────────────────────────────────
UninstallDisplayName=GestionnaireRH - Système de Gestion RH
UninstallDisplayIcon={app}\GestionnaireRH.exe
CreateUninstallRegKey=yes

; ── Version info (visible dans Programmes et fonctionnalités) ──────────────────
VersionInfoVersion=1.1.0.0
VersionInfoCompany=Guinée RH
VersionInfoDescription=GestionnaireRH - Système de Gestion des Ressources Humaines
VersionInfoCopyright=Copyright © 2024-2026 Guinée RH

[Languages]
Name: "french"; MessagesFile: "compiler:Languages\French.isl"

[Tasks]
Name: "desktopicon";   Description: "Créer un raccourci sur le Bureau";         GroupDescription: "Raccourcis :"
Name: "startmenuicon"; Description: "Créer une entrée dans le menu Démarrer";   GroupDescription: "Raccourcis :"
Name: "autostart";     Description: "Lancer GestionnaireRH au démarrage de Windows"; GroupDescription: "Options :"; Flags: unchecked

[Files]
; Application compilée (tout le dossier dist\GestionnaireRH)
; ignoreversion + recursesubdirs : écrase les anciens fichiers lors d'une mise à jour
Source: "dist\GestionnaireRH\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs; Excludes: "license.dat"

; Script de désinstallation
Source: "desinstaller.bat"; DestDir: "{app}"; Flags: ignoreversion

; Script d'arrêt du serveur (raccourci menu Démarrer)
Source: "Arreter_GestionnaireRH.bat"; DestDir: "{app}"; Flags: ignoreversion

; Base de données template pré-migrée (première installation uniquement)
Source: "dist\GestionnaireRH\db_template.sqlite3"; DestDir: "{app}"; Flags: onlyifdoesntexist

; NOTE: license_manager.py n'est plus copié en source (sécurité).
; Le technicien utilise le script d'activation depuis la machine propriétaire.

; Protection anti-vol et anti-falsification (ICG Guinea)
; IMPORTANT : project_guardian.pyd, runtime_shield.pyd et license_manager.pyd
; sont déjà dans dist\GestionnaireRH\_internal\ (compilés Nuitka via le build).
; NE PAS copier les .py sources dans _internal — runtime_shield.py les détecterait
; comme une falsification et bloquerait l'application.
; NOTE: .integrity_manifest.json retiré de l'installeur (causait des faux positifs).
; La protection repose sur les checksums runtime_shield générés par protect_distribution.py.

; Icône
Source: "static\img\logo.ico"; DestDir: "{app}"; Flags: ignoreversion

[Dirs]
; Dossiers avec permissions d'écriture
Name: "{app}\logs";               Permissions: users-modify
Name: "{app}\media";              Permissions: users-modify
Name: "{app}\media\photos";       Permissions: users-modify
Name: "{app}\backups";            Permissions: users-modify
Name: "{app}\staticfiles";        Permissions: users-modify
Name: "{app}\data";               Permissions: users-modify

[Icons]
; Bureau
Name: "{autodesktop}\GestionnaireRH"; Filename: "{app}\GestionnaireRH.exe"; WorkingDir: "{app}"; IconFilename: "{app}\logo.ico"; Comment: "GestionnaireRH - Gestion des Ressources Humaines"; Tasks: desktopicon

; Menu Démarrer
Name: "{group}\GestionnaireRH";                       Filename: "{app}\GestionnaireRH.exe";         WorkingDir: "{app}"; IconFilename: "{app}\logo.ico"; Comment: "Démarrer GestionnaireRH"
Name: "{group}\Arrêter GestionnaireRH";               Filename: "{app}\Arreter_GestionnaireRH.bat"; WorkingDir: "{app}"; Comment: "Arrêter le serveur GestionnaireRH"
Name: "{group}\{cm:UninstallProgram,GestionnaireRH}"; Filename: "{uninstallexe}"

; Démarrage automatique (optionnel)
Name: "{userstartup}\GestionnaireRH"; Filename: "{app}\GestionnaireRH.exe"; WorkingDir: "{app}"; Tasks: autostart

[Registry]
; Enregistrement pour le panneau "Programmes et fonctionnalités"
Root: HKCU; Subkey: "Software\Guinée RH\GestionnaireRH"; ValueType: string; ValueName: "Version";    ValueData: "1.1.0"; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\Guinée RH\GestionnaireRH"; ValueType: string; ValueName: "InstallDir"; ValueData: "{app}";  Flags: uninsdeletevalue

[Run]
; Proposer de lancer l'application après installation
Filename: "{app}\GestionnaireRH.exe"; Description: "Démarrer GestionnaireRH maintenant"; WorkingDir: "{app}"; Flags: nowait postinstall skipifsilent

[UninstallRun]
; Arrêter le serveur avant la désinstallation
Filename: "taskkill"; Parameters: "/F /IM GestionnaireRH.exe"; Flags: runhidden; RunOnceId: "KillServer"

[InstallDelete]
; Nettoyer les anciens .py sources dans _internal (résidus des versions précédentes)
; Ces fichiers doivent être compilés en .pyd/.pyc, pas en texte clair.
Type: files; Name: "{app}\_internal\license_manager.py"
Type: files; Name: "{app}\_internal\project_guardian.py"
Type: files; Name: "{app}\_internal\runtime_shield.py"
Type: files; Name: "{app}\_internal\run_server.py"
Type: files; Name: "{app}\_internal\manage.py"
; Nettoyer les anciens .pyc résiduels (seront recréés par le nouveau build)
Type: files; Name: "{app}\_internal\run_server.pyc"
Type: files; Name: "{app}\_internal\manage.pyc"
; Nettoyer une ancienne migration residuelle qui creait un conflit de graphe paie.
Type: files; Name: "{app}\_internal\paie\migrations\0134_set_seuil_ta_onfpp_30.py"
Type: files; Name: "{app}\_internal\paie\migrations\0134_set_seuil_ta_onfpp_30.pyc"
Type: files; Name: "{app}\_internal\paie\migrations\__pycache__\0134_set_seuil_ta_onfpp_30*.pyc"
; Nettoyer les anciens sous-dossiers .py résiduels
Type: filesandordirs; Name: "{app}\_internal\core\*.py"
Type: filesandordirs; Name: "{app}\_internal\employes\*.py"
Type: filesandordirs; Name: "{app}\_internal\paie\*.py"
Type: filesandordirs; Name: "{app}\_internal\dashboard\*.py"
Type: filesandordirs; Name: "{app}\_internal\gestionnaire_rh\*.py"
; Nettoyer le marqueur de falsification (réinitialisation à la mise à jour)
Type: files; Name: "{app}\.tamper_detected"
; Nettoyer les anciens fichiers de protection (seront recréés par le nouveau build)
; IMPORTANT : ces fichiers contiennent les checksums de l'ancien build et DOIVENT
; être supprimés avant l'installation du nouveau, sinon tamper detection se déclenche.
Type: files; Name: "{app}\_internal\.file_checksums"
Type: files; Name: "{app}\_internal\.exe_signature"
Type: files; Name: "{app}\_internal\.protection_report.json"
Type: files; Name: "{app}\_internal\.integrity_manifest.json"
; Nettoyer l'ancien manifest à la racine (sera remplacé par le nouveau)
Type: files; Name: "{app}\.integrity_manifest.json"
; Nettoyer les anciens .py sources à la RACINE de {app} (résidus critiques)
Type: files; Name: "{app}\license_manager.py"
Type: files; Name: "{app}\project_guardian.py"
Type: files; Name: "{app}\runtime_shield.py"
Type: files; Name: "{app}\run_server.py"

[UninstallDelete]
Type: filesandordirs; Name: "{app}\logs"
Type: filesandordirs; Name: "{app}\staticfiles"
Type: filesandordirs; Name: "{app}\__pycache__"
Type: files;          Name: "{app}\.secret_key"
Type: files;          Name: "{app}\.trial_start"
Type: files;          Name: "{app}\install_path.txt"

[Messages]
WelcomeLabel1=Bienvenue dans l'assistant d'installation de GestionnaireRH
WelcomeLabel2=Ce programme va installer ou mettre à jour GestionnaireRH - Système de Gestion des Ressources Humaines sur votre ordinateur.%n%nGestionnaireRH est une solution complète de gestion RH développée par Guinée RH. Elle fonctionne entièrement hors ligne.%n%nSi une version précédente est installée, vos données seront conservées.%n%nFermez toutes les autres applications avant de continuer.
FinishedHeadingLabel=Installation de GestionnaireRH terminée !
FinishedLabel=GestionnaireRH a été installé / mis à jour avec succès sur votre ordinateur.%n%nIdentifiants par défaut (première installation) :%n  Utilisateur : admin%n  Mot de passe  : admin1234%n%nL'application s'ouvre dans votre navigateur sur http://127.0.0.1:8000%n%nNOTE : Si c'est une mise à jour, vos données et votre licence sont conservées.

[Code]

var
  LicenseChoicePage: TInputOptionWizardPage;
  LicenseFilePage: TInputFileWizardPage;
  SelectedLicenseFile: String;
  UseProvidedLicense: Boolean;

// ── Vérifier si l'application est en cours d'exécution ────────────────────────
function IsAppRunning(): Boolean;
var
  ResultCode: Integer;
begin
  Result := False;
  // findstr retourne 0 si trouvé, 1 si non trouvé (contrairement à tasklist seul qui retourne toujours 0)
  if Exec('cmd.exe', '/C tasklist /FI "IMAGENAME eq GestionnaireRH.exe" /NH | findstr /I "GestionnaireRH.exe"', '', SW_HIDE, ewWaitUntilTerminated, ResultCode) then
    Result := (ResultCode = 0);
end;

function InitializeSetup(): Boolean;
var
  ResultCode: Integer;
  PrevVersion: String;
begin
  Result := True;

  // Détecter une installation existante et afficher un message de mise à jour
  if RegQueryStringValue(HKCU, 'Software\Guinée RH\GestionnaireRH', 'Version', PrevVersion) then
  begin
    if MsgBox(
      'GestionnaireRH version ' + PrevVersion + ' est déjà installé.' + #13#10 +
      'Voulez-vous mettre à jour vers la version 1.1.0 ?' + #13#10 + #13#10 +
      'Vos données (base de données, licences, médias) seront conservées.',
      mbConfirmation, MB_YESNO
    ) = IDNO then
    begin
      Result := False;
      Exit;
    end;
  end;

  // Fermer automatiquement l'application si elle tourne
  if IsAppRunning() then
  begin
    MsgBox(
      'GestionnaireRH est en cours d''exécution.' + #13#10 +
      'L''application va être fermée automatiquement pour la mise à jour.',
      mbInformation, MB_OK
    );
    Exec('taskkill', '/F /IM GestionnaireRH.exe', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
    Sleep(2000);
  end;
end;

// ── Afficher l'ID machine à la fin pour l'activation ─────────────────────────
procedure InitializeWizard();
begin
  LicenseChoicePage := CreateInputOptionPage(
    wpSelectTasks,
    'Activation de licence',
    'Choisissez le mode d''activation de GestionnaireRH.',
    'Si vous avez deja recu un fichier de licence, vous pouvez l''ajouter maintenant. ' +
    'Sinon, l''application demarrera en version d''essai de 30 jours.',
    True,
    False
  );
  LicenseChoicePage.Add('J''ai un fichier de licence (.lic ou license.dat)');
  LicenseChoicePage.Add('Continuer avec la version d''essai de 30 jours');
  LicenseChoicePage.SelectedValueIndex := 1;

  LicenseFilePage := CreateInputFilePage(
    LicenseChoicePage.ID,
    'Ajouter la licence',
    'Selectionnez le fichier de licence fourni par Guinee RH.',
    'Le fichier sera installe automatiquement sous le nom license.dat dans le dossier de l''application.'
  );
  LicenseFilePage.Add(
    'Fichier de licence :',
    'Fichiers licence (*.lic;license.dat)|*.lic;license.dat|Tous les fichiers (*.*)|*.*',
    '.lic'
  );
end;

function ShouldSkipPage(PageID: Integer): Boolean;
begin
  Result := False;
  if Assigned(LicenseFilePage) and (PageID = LicenseFilePage.ID) then
    Result := LicenseChoicePage.SelectedValueIndex <> 0;
end;

function LooksLikeLicenseFile(FileName: String): Boolean;
var
  Content: AnsiString;
begin
  Result := False;
  if not LoadStringFromFile(FileName, Content) then
    Exit;

  Result :=
    (Pos('"license_data"', Content) > 0) and
    (Pos('"signature"', Content) > 0) and
    (Pos('"version"', Content) > 0);
end;

function NextButtonClick(CurPageID: Integer): Boolean;
var
  LicenseFile: String;
begin
  Result := True;

  if Assigned(LicenseChoicePage) and (CurPageID = LicenseChoicePage.ID) then
  begin
    UseProvidedLicense := False;
    SelectedLicenseFile := '';
  end;

  if Assigned(LicenseFilePage) and (CurPageID = LicenseFilePage.ID) then
  begin
    LicenseFile := Trim(LicenseFilePage.Values[0]);
    if LicenseFile = '' then
    begin
      MsgBox(
        'Veuillez selectionner votre fichier de licence, ou revenez a l''etape precedente pour choisir la version d''essai.',
        mbError,
        MB_OK
      );
      Result := False;
      Exit;
    end;

    if not FileExists(LicenseFile) then
    begin
      MsgBox('Le fichier de licence selectionne est introuvable.', mbError, MB_OK);
      Result := False;
      Exit;
    end;

    if not LooksLikeLicenseFile(LicenseFile) then
    begin
      MsgBox(
        'Ce fichier ne ressemble pas a une licence GestionnaireRH valide.' + #13#10 +
        'Selectionnez un fichier .lic ou license.dat fourni par Guinee RH.',
        mbError,
        MB_OK
      );
      Result := False;
      Exit;
    end;

    SelectedLicenseFile := LicenseFile;
    UseProvidedLicense := True;
  end;
end;

function GetMachineId(): String;
var
  MachineGuid: String;
begin
  if RegQueryStringValue(HKEY_LOCAL_MACHINE,
    'SOFTWARE\Microsoft\Cryptography', 'MachineGuid', MachineGuid) then
    Result := MachineGuid
  else
    Result := 'Indisponible';
end;

procedure CurPageChanged(CurPageID: Integer);
begin
  // Rien ici — placeholder pour extensions futures
end;

// ── Sauvegarde de la base de données avant désinstallation ───────────────────
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  DbPath:     String;
  BackupDir:  String;
  BackupPath: String;
begin
  if CurUninstallStep = usUninstall then
  begin
    DbPath := ExpandConstant('{app}\db.sqlite3');
    if FileExists(DbPath) then
    begin
      if MsgBox(
        'Voulez-vous sauvegarder votre base de données avant la désinstallation ?' + #13#10 + #13#10 +
        'La sauvegarde sera placée dans :' + #13#10 +
        ExpandConstant('{userdocs}\GestionnaireRH_Backup'),
        mbConfirmation, MB_YESNO
      ) = IDYES then
      begin
        BackupDir  := ExpandConstant('{userdocs}\GestionnaireRH_Backup');
        ForceDirectories(BackupDir);
        BackupPath := BackupDir + '\db_backup_' +
                      GetDateTimeString('yyyymmdd_hhnnss', #0, #0) + '.sqlite3';
        CopyFile(DbPath, BackupPath, False);
        MsgBox(
          'Base de données sauvegardée dans :' + #13#10 + BackupPath,
          mbInformation, MB_OK
        );
      end;
    end;
  end;
end;

// ── Sauvegarde BDD avant mise à jour + création dossiers après install ────────
procedure CurStepChanged(CurStep: TSetupStep);
var
  DbPath: String;
  BackupDir: String;
  BackupPath: String;
  LicensePath: String;
  LicenseBackup: String;
begin
  if CurStep = ssInstall then
  begin
    // Sauvegarder la base de données si elle existe (mise à jour)
    DbPath := ExpandConstant('{app}\db.sqlite3');
    if FileExists(DbPath) then
    begin
      BackupDir := ExpandConstant('{app}\backups');
      ForceDirectories(BackupDir);
      BackupPath := BackupDir + '\db_pre_update_' +
                    GetDateTimeString('yyyymmdd_hhnnss', #0, #0) + '.sqlite3';
      CopyFile(DbPath, BackupPath, False);
      Log('Base de données sauvegardée dans : ' + BackupPath);
    end;
    // Sauvegarder la licence si elle existe
    LicensePath := ExpandConstant('{app}\license.dat');
    if FileExists(LicensePath) then
    begin
      LicenseBackup := ExpandConstant('{app}\backups\license_backup.dat');
      CopyFile(LicensePath, LicenseBackup, False);
      Log('Licence sauvegardée dans : ' + LicenseBackup);
    end;
  end;

  if CurStep = ssPostInstall then
  begin
    ForceDirectories(ExpandConstant('{app}\logs'));
    ForceDirectories(ExpandConstant('{app}\media'));
    ForceDirectories(ExpandConstant('{app}\backups'));
    ForceDirectories(ExpandConstant('{app}\data'));

    // Restaurer la licence si elle a été écrasée
    if UseProvidedLicense and FileExists(SelectedLicenseFile) then
    begin
      LicensePath := ExpandConstant('{app}\license.dat');
      if CopyFile(SelectedLicenseFile, LicensePath, False) then
        Log('Licence installee depuis : ' + SelectedLicenseFile)
      else
        MsgBox(
          'La licence n''a pas pu etre copiee dans le dossier d''installation.' + #13#10 +
          'Vous pourrez toujours l''activer plus tard depuis l''application.',
          mbError,
          MB_OK
        );
    end;

    LicensePath := ExpandConstant('{app}\license.dat');
    LicenseBackup := ExpandConstant('{app}\backups\license_backup.dat');
    if (not UseProvidedLicense) and (not FileExists(LicensePath)) and FileExists(LicenseBackup) then
    begin
      CopyFile(LicenseBackup, LicensePath, False);
      Log('Licence restaurée depuis la sauvegarde');
    end;
  end;
end;

// ── Message récapitulatif avec infos licence ──────────────────────────────────
function UpdateReadyMemo(Space, NewLine, MemoUserInfoInfo, MemoDirInfo,
  MemoTypeInfo, MemoComponentsInfo, MemoGroupInfo, MemoTasksInfo: String): String;
begin
  Result := MemoDirInfo + NewLine + NewLine +
            MemoGroupInfo + NewLine + NewLine +
            MemoTasksInfo + NewLine + NewLine +
            '─────────────────────────────────────────' + NewLine +
            'ACTIVATION DE LICENCE' + NewLine +
            'Une période d''essai de 30 jours démarre automatiquement.' + NewLine +
            'Pour activer votre licence permanente, notez votre ID Machine' + NewLine +
            'et contactez Guinée RH.' + NewLine +
            '─────────────────────────────────────────';
end;
