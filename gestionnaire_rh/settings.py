"""
Django settings for gestionnaire_rh project.
"""

from pathlib import Path
from decouple import config
import os
import sys

# Détection PyInstaller
PYINSTALLER_MODE = getattr(sys, 'frozen', False)

# Build paths inside the project like this: BASE_DIR / 'subdir'.
if PYINSTALLER_MODE:
    # En mode PyInstaller, utiliser le répertoire de l'exécutable
    BASE_DIR = Path(os.path.dirname(sys.executable))
    # Répertoire interne de PyInstaller (_MEIPASS)
    INTERNAL_DIR = Path(sys._MEIPASS)
else:
    BASE_DIR = Path(__file__).resolve().parent.parent
    INTERNAL_DIR = BASE_DIR

# SECURITY WARNING: keep the secret key used in production secret!
SECRET_KEY = config('SECRET_KEY', default='django-insecure-change-this-in-production')

# SECURITY WARNING: don't run with debug turned on in production!
# En mode PyInstaller, forcer DEBUG=True pour servir les fichiers statiques
if PYINSTALLER_MODE:
    DEBUG = True
else:
    DEBUG = config('DEBUG', default=False, cast=bool)

# Bloquer temporairement les inscriptions (True = inscriptions bloquées)
REGISTRATION_DISABLED = config('REGISTRATION_DISABLED', default=True, cast=bool)

# Code de vérification administrateur pour créer un compte
ADMIN_REGISTRATION_CODE = config('ADMIN_REGISTRATION_CODE', default='625196629')

ALLOWED_HOSTS = ['www.guineerh.space', 'guineerh.space', 'guineerh.pythonanywhere.com', 'www.icguinea.com', 'icguinea.com', 'siteicguinea.pythonanywhere.com', 'localhost', '127.0.0.1', 'testserver']

# Application definition
INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'django.contrib.humanize',
    
    # Third party apps
    'crispy_forms',
    'crispy_bootstrap5',
    'django_filters',
    'widget_tweaks',
    'import_export',
    'rest_framework',
    'corsheaders',
    'axes',
    'csp',
    'django_permissions_policy',
    
    # Local apps
    'core',
    'employes',
    'paie',
    'temps_travail',
    'conges',
    'contrats',
    'recrutement',
    'formation',
    'dashboard',
    'payments',
    'portail',
    'comptabilite',
]

MIDDLEWARE = [
    'gestionnaire_rh.static_middleware.PyInstallerStaticMiddleware',
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'csp.middleware.CSPMiddleware',
    'core.middleware.SecurityHeadersMiddleware',
    'django.middleware.gzip.GZipMiddleware',  # Compression GZip pour rapidité
    'django.contrib.sessions.middleware.SessionMiddleware',
    'corsheaders.middleware.CorsMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
    # Security middlewares
    'axes.middleware.AxesMiddleware',
    'django_permissions_policy.PermissionsPolicyMiddleware',
    'core.middleware.SQLInjectionProtectionMiddleware',
    'core.middleware.XSSProtectionMiddleware',
    'core.middleware.RequestLoggingMiddleware',
    # Multi-company middleware
    'core.middleware.EntrepriseQuotaMiddleware',
    # Protection anti-falsification
    'core.middleware_guardian.ProjectIntegrityMiddleware',
    # Licence middleware
    'core.middleware_licence.LicenceMiddleware',
]

ROOT_URLCONF = 'gestionnaire_rh.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [INTERNAL_DIR / 'templates'] if PYINSTALLER_MODE else [BASE_DIR / 'templates'],
        'APP_DIRS': not PYINSTALLER_MODE,  # Désactivé en mode PyInstaller (cached.Loader utilisé)
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
                'core.context_processors.company_info',
            ],
            **(
                {
                    'loaders': [
                        ('django.template.loaders.cached.Loader', [
                            'django.template.loaders.filesystem.Loader',
                            'django.template.loaders.app_directories.Loader',
                        ]),
                    ],
                } if PYINSTALLER_MODE else {}
            ),
        },
    },
]

WSGI_APPLICATION = 'gestionnaire_rh.wsgi.application'

# Database
# Use SQLite by default for development, PostgreSQL/MySQL can be configured via environment variables
DB_ENGINE = config('DB_ENGINE', default='sqlite')

if DB_ENGINE == 'postgresql':
    DATABASES = {
        'default': {
        'CONN_MAX_AGE': 0,
            'ENGINE': 'django.db.backends.postgresql',
            'NAME': config('DB_NAME', default='gestionnaire_rh_guinee'),
            'USER': config('DB_USER', default='postgres'),
            'PASSWORD': config('DB_PASSWORD', default=''),
            'HOST': config('DB_HOST', default='localhost'),
            'PORT': config('DB_PORT', default='5432'),
        'OPTIONS': {
            'init_command': "SET sql_mode='STRICT_TRANS_TABLES'",
        },
        }
    }
elif DB_ENGINE == 'mysql':
    DATABASES = {
        'default': {
        'CONN_MAX_AGE': 0,
            'ENGINE': 'django.db.backends.mysql',
            'NAME': config('DB_NAME'),
            'USER': config('DB_USER'),
            'PASSWORD': config('DB_PASSWORD'),
            'HOST': config('DB_HOST', default='localhost'),
            'PORT': config('DB_PORT', default='3306'),
            'OPTIONS': {
                'charset': 'utf8mb4',
                'init_command': "SET sql_mode='STRICT_TRANS_TABLES'",
            }
        }
    }
else:
    DATABASES = {
        'default': {
        'CONN_MAX_AGE': 0,
            'ENGINE': 'django.db.backends.sqlite3',
            'NAME': BASE_DIR / 'db.sqlite3',
            'OPTIONS': {
                'timeout': 20,
            }
        }
    }

# Activer WAL mode pour SQLite (évite "database is locked")
from django.db.backends.signals import connection_created

def _activate_wal_mode(sender, connection, **kwargs):
    if connection.vendor == 'sqlite':
        cursor = connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL;")

connection_created.connect(_activate_wal_mode)

# Password validation
AUTH_PASSWORD_VALIDATORS = [
    {
        'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
        'OPTIONS': {
            'min_length': 8,
        }
    },
    {
        'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator',
    },
]

# Internationalization
LANGUAGE_CODE = 'fr-fr'
TIME_ZONE = 'Africa/Conakry'
USE_I18N = True
USE_TZ = True

# Static files (CSS, JavaScript, Images)
STATIC_URL = '/static/'
if PYINSTALLER_MODE:
    # En mode PyInstaller, chercher static dans plusieurs emplacements possibles
    STATIC_ROOT = BASE_DIR / 'staticfiles'
    _possible_static = [
        INTERNAL_DIR / 'static',
        BASE_DIR / '_internal' / 'static',
        BASE_DIR / 'static',
    ]
    STATICFILES_DIRS = [p for p in _possible_static if p.exists()]
    # Désactiver le manifest storage en mode PyInstaller
    STATICFILES_STORAGE = 'django.contrib.staticfiles.storage.StaticFilesStorage'
else:
    STATIC_ROOT = BASE_DIR / 'staticfiles'
    STATICFILES_DIRS = [BASE_DIR / 'static']
    if DEBUG:
        STATICFILES_STORAGE = 'django.contrib.staticfiles.storage.StaticFilesStorage'
    else:
        STATICFILES_STORAGE = 'whitenoise.storage.CompressedManifestStaticFilesStorage'

# Media files
MEDIA_URL = '/media/'
MEDIA_ROOT = BASE_DIR / 'media'

# Default primary key field type
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# Custom User Model
AUTH_USER_MODEL = 'core.Utilisateur'

# Crispy Forms
CRISPY_ALLOWED_TEMPLATE_PACKS = "bootstrap5"
CRISPY_TEMPLATE_PACK = "bootstrap5"

# Login URLs
LOGIN_URL = 'core:login'
LOGIN_REDIRECT_URL = 'dashboard:index'
LOGOUT_REDIRECT_URL = 'core:login'

# Email Configuration
EMAIL_BACKEND = config('EMAIL_BACKEND', default='django.core.mail.backends.console.EmailBackend')
EMAIL_HOST = config('EMAIL_HOST', default='smtp.gmail.com')
EMAIL_PORT = config('EMAIL_PORT', default=587, cast=int)
EMAIL_USE_TLS = config('EMAIL_USE_TLS', default=True, cast=bool)
EMAIL_HOST_USER = config('EMAIL_HOST_USER', default='')
EMAIL_HOST_PASSWORD = config('EMAIL_HOST_PASSWORD', default='')

# Paycard Payment Gateway (Guinée)
PAYCARD_API_KEY = config('PAYCARD_API_KEY', default='')

# Celery Configuration
# CELERY_BROKER_URL = config('REDIS_URL', default='redis://localhost:6379/0')
# CELERY_RESULT_BACKEND = config('REDIS_URL', default='redis://localhost:6379/0')
CELERY_ACCEPT_CONTENT = ['json']
CELERY_TASK_SERIALIZER = 'json'
CELERY_RESULT_SERIALIZER = 'json'
CELERY_TIMEZONE = TIME_ZONE

# REST Framework
REST_FRAMEWORK = {
    'DEFAULT_PERMISSION_CLASSES': [
        'rest_framework.permissions.IsAuthenticated',
    ],
    'DEFAULT_AUTHENTICATION_CLASSES': [
        'rest_framework.authentication.SessionAuthentication',
    ],
    'DEFAULT_PAGINATION_CLASS': 'rest_framework.pagination.PageNumberPagination',
    'PAGE_SIZE': 50,
}

# CORS
CORS_ALLOW_ALL_ORIGINS = DEBUG
CORS_ALLOWED_ORIGINS = [
    "http://localhost:3000",
    "http://127.0.0.1:3000",
]

# Company Settings
COMPANY_NAME = config('COMPANY_NAME', default='Gestionnaire RH Guinée')
COMPANY_NIF = config('COMPANY_NIF', default='')
COMPANY_CNSS = config('COMPANY_CNSS', default='')

# Paie Settings
SMIG_GUINEE = 440000  # SMIG en GNF
TAUX_CNSS_EMPLOYE = 5.0  # 5%
TAUX_CNSS_EMPLOYEUR = 18.0  # 18%
TAUX_INAM = 2.5  # 2.5%
CONGES_ANNUELS = 26  # 26 jours selon Code du Travail Guinée
HEURES_MENSUELLES = 173.33  # Heures de travail par mois
JOURS_TRAVAIL_MOIS = 22  # Jours de travail par mois

# ============================================================================
# SECURITY SETTINGS
# ============================================================================

# HTTPS and Security
SECURE_SSL_REDIRECT = config('SECURE_SSL_REDIRECT', default=False, cast=bool)
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
SECURE_HSTS_SECONDS = config('SECURE_HSTS_SECONDS', default=31536000, cast=int)  # 1 year
SECURE_HSTS_INCLUDE_SUBDOMAINS = config('SECURE_HSTS_INCLUDE_SUBDOMAINS', default=True, cast=bool)
SECURE_HSTS_PRELOAD = config('SECURE_HSTS_PRELOAD', default=True, cast=bool)
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_BROWSER_XSS_FILTER = True

# Session Security
if PYINSTALLER_MODE:
    SESSION_COOKIE_SECURE = False
else:
    SESSION_COOKIE_SECURE = config('SESSION_COOKIE_SECURE', default=True, cast=bool)
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = 'Strict'
SESSION_COOKIE_AGE = 8 * 60 * 60  # 8 heures
SESSION_SAVE_EVERY_REQUEST = False  # Sauvegarder la session uniquement quand modifiée
SESSION_EXPIRE_AT_BROWSER_CLOSE = True

# CSRF Protection
if PYINSTALLER_MODE:
    CSRF_COOKIE_SECURE = False
else:
    CSRF_COOKIE_SECURE = config('CSRF_COOKIE_SECURE', default=True, cast=bool)
CSRF_COOKIE_HTTPONLY = True
CSRF_COOKIE_SAMESITE = 'Strict'
CSRF_USE_SESSIONS = False
CSRF_FAILURE_VIEW = 'core.views.csrf_failure'

# Clickjacking Protection
X_FRAME_OPTIONS = 'DENY'

# Referrer & Permissions Policy
SECURE_REFERRER_POLICY = 'strict-origin-when-cross-origin'
PERMISSIONS_POLICY = {
    'accelerometer': [],
    'camera': [],
    'geolocation': [],
    'gyroscope': [],
    'magnetometer': [],
    'microphone': [],
    'payment': [],
    'usb': [],
    'autoplay': [],
    'fullscreen': ['self'],
}

# Content Security Policy (django-csp 4.0+ format)
# Configuration assouplie pour la production
# Note: 'unsafe-eval' est nécessaire pour certaines bibliothèques JS (ex: Chart.js, certains plugins Bootstrap)
# Utilisez avec précaution et évitez eval() dans votre propre code
CONTENT_SECURITY_POLICY = {
    'DIRECTIVES': {
        'default-src': ("'self'",),
        'script-src': ("'self'", "'unsafe-inline'", "'unsafe-eval'", "https://cdn.jsdelivr.net", "https://code.jquery.com", "https://stackpath.bootstrapcdn.com"),
        'style-src': ("'self'", "'unsafe-inline'", "https://cdn.jsdelivr.net", "https://fonts.googleapis.com", "https://stackpath.bootstrapcdn.com"),
        'font-src': ("'self'", "https://fonts.gstatic.com", "https://cdn.jsdelivr.net"),
        'img-src': ("'self'", "data:", "https:", "blob:"),
        'connect-src': ("'self'",),
        'frame-ancestors': ("'none'",),
        'base-uri': ("'self'",),
        'form-action': ("'self'",),
        'media-src': ("'self'", "data:", "https:"),
        'object-src': ("'none'",),
        'worker-src': ("'self'", "blob:"),
    }
}

# Authentication Backends
AUTHENTICATION_BACKENDS = [
    'axes.backends.AxesStandaloneBackend',
    'django.contrib.auth.backends.ModelBackend',
]

# Django Axes - Protection contre les attaques par force brute
AXES_ENABLED = True
AXES_FAILURE_LIMIT = 5  # Nombre de tentatives avant blocage
AXES_COOLOFF_TIME = 1  # Temps de blocage en heures
AXES_LOCK_OUT_AT_FAILURE = True
AXES_LOCKOUT_TEMPLATE = 'core/account_locked.html'
AXES_RESET_ON_SUCCESS = True
# Offline build: django-axes AccessLog has legacy DB variants with session_hash NOT NULL.
# Disabling successful-login logs keeps brute-force protection active without breaking login.
AXES_DISABLE_ACCESS_LOG = True
AXES_LOCKOUT_PARAMETERS = ['username', 'ip_address']
AXES_IPWARE_PROXY_COUNT = 1
AXES_IPWARE_META_PRECEDENCE_ORDER = [
    'HTTP_X_FORWARDED_FOR',
    'X_FORWARDED_FOR',
    'HTTP_CLIENT_IP',
    'HTTP_X_REAL_IP',
    'HTTP_X_FORWARDED',
    'HTTP_X_CLUSTER_CLIENT_IP',
    'HTTP_FORWARDED_FOR',
    'HTTP_FORWARDED',
    'REMOTE_ADDR',
]

# Django Defender - Protection supplémentaire
# DEFENDER_REDIS_URL = config('REDIS_URL', default='redis://localhost:6379/1')
DEFENDER_LOGIN_FAILURE_LIMIT = 5
DEFENDER_BEHIND_REVERSE_PROXY = True
DEFENDER_DISABLE_IP_LOCKOUT = False
DEFENDER_DISABLE_USERNAME_LOCKOUT = False
DEFENDER_COOLOFF_TIME = 3600  # 1 hour in seconds
DEFENDER_LOCKOUT_TEMPLATE = 'core/account_locked.html'

# Encryption
ENCRYPTION_KEY = config('ENCRYPTION_KEY', default=None)

# IP Whitelist (optionnel - désactivé par défaut)
IP_WHITELIST_ENABLED = config('IP_WHITELIST_ENABLED', default=False, cast=bool)
IP_WHITELIST = config('IP_WHITELIST', default='').split(',') if config('IP_WHITELIST', default='') else []

# Security Logging
SECURITY_LOGGING_ENABLED = True

# File Upload Security
FILE_UPLOAD_MAX_MEMORY_SIZE = 5242880  # 5MB
DATA_UPLOAD_MAX_MEMORY_SIZE = 5242880  # 5MB
FILE_UPLOAD_PERMISSIONS = 0o644
FILE_UPLOAD_DIRECTORY_PERMISSIONS = 0o755
FILE_UPLOAD_HANDLERS = [
    'django.core.files.uploadhandler.TemporaryFileUploadHandler',
]

# Allowed file extensions
ALLOWED_UPLOAD_EXTENSIONS = ['jpg', 'jpeg', 'png', 'pdf', 'doc', 'docx']

# Database Performance & Security
if PYINSTALLER_MODE:
    # Mode offline : connexion persistante (un seul utilisateur local)
    DATABASES['default']['CONN_MAX_AGE'] = 0  # Pas de connexion persistante
elif not DEBUG:
    DATABASES['default']['CONN_MAX_AGE'] = 0  # Pas de connexion persistante
    if DATABASES['default']['ENGINE'] == 'django.db.backends.sqlite3':
        DATABASES['default'].setdefault('OPTIONS', {})['timeout'] = 20
    else:
        DATABASES['default'].setdefault('OPTIONS', {})['connect_timeout'] = 10
else:
    DATABASES['default']['CONN_MAX_AGE'] = 0  # Pas de persistence en dev

# Logging Configuration
LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'verbose': {
            'format': '{levelname} {asctime} {module} {process:d} {thread:d} {message}',
            'style': '{',
        },
        'simple': {
            'format': '{levelname} {message}',
            'style': '{',
        },
    },
    'filters': {
        'require_debug_false': {
            '()': 'django.utils.log.RequireDebugFalse',
        },
        'require_debug_true': {
            '()': 'django.utils.log.RequireDebugTrue',
        },
    },
    'handlers': {
        'console': {
            'level': 'INFO',
            'class': 'logging.StreamHandler',
            'formatter': 'simple'
        },
        'file': {
            'level': 'WARNING',
            'class': 'logging.handlers.RotatingFileHandler',
            'filename': BASE_DIR / 'logs' / 'django.log',
            'maxBytes': 1024 * 1024 * 15,  # 15MB
            'backupCount': 10,
            'formatter': 'verbose',
        },
        'security_file': {
            'level': 'WARNING',
            'class': 'logging.handlers.RotatingFileHandler',
            'filename': BASE_DIR / 'logs' / 'security.log',
            'maxBytes': 1024 * 1024 * 15,  # 15MB
            'backupCount': 10,
            'formatter': 'verbose',
        },
        'mail_admins': {
            'level': 'ERROR',
            'class': 'django.utils.log.AdminEmailHandler',
            'filters': ['require_debug_false'],
        }
    },
    'loggers': {
        'django': {
            'handlers': ['console', 'file'],
            'level': 'INFO',
            'propagate': False,
        },
        'django.security': {
            'handlers': ['security_file', 'mail_admins'],
            'level': 'WARNING',
            'propagate': False,
        },
        'core.middleware': {
            'handlers': ['security_file'],
            'level': 'WARNING',
            'propagate': False,
        },
        'axes': {
            'handlers': ['security_file'],
            'level': 'WARNING',
            'propagate': False,
        },
        'defender': {
            'handlers': ['security_file'],
            'level': 'WARNING',
            'propagate': False,
        },
    },
}

# Create logs directory if it doesn't exist
import os
logs_dir = BASE_DIR / 'logs'
if not os.path.exists(logs_dir):
    os.makedirs(logs_dir)

# Admin email for security alerts
ADMINS = [
    ('Admin', config('ADMIN_EMAIL', default='admin@example.com')),
]
MANAGERS = ADMINS


# Disable Redis-dependent features (not available on PythonAnywhere free tier)
DEFENDER_DISABLE_IP_LOCKOUT = True
DEFENDER_DISABLE_USERNAME_LOCKOUT = True
DEFENDER_STORE_ACCESS_ATTEMPTS = False

# CSRF Trusted Origins
CSRF_TRUSTED_ORIGINS = [
    'https://www.guineerh.space',
    'https://guineerh.space',
]

if PYINSTALLER_MODE:
    CSRF_TRUSTED_ORIGINS += [
        'http://127.0.0.1:8000',
        'http://localhost:8000',
    ]

# ============================================================================
# PERFORMANCE OPTIMIZATIONS
# ============================================================================

# Cache Configuration
CACHES = {
    'default': {

        'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
        'LOCATION': 'gestionnaire-rh-cache',
        'TIMEOUT': 3600,  # 1 heure par défaut
        'OPTIONS': {
            'MAX_ENTRIES': 5000,
            'CULL_FREQUENCY': 3,  # 1/3 des entrées supprimées quand MAX atteint
        }
    }
}


# if not DEBUG:

#         'default': {

#             'BACKEND': 'django.core.cache.backends.redis.RedisCache',
#             'LOCATION': config('REDIS_URL', default='redis://127.0.0.1:6379/1'),
#             'TIMEOUT': 3600,
#             'OPTIONS': {
#                 'CLIENT_CLASS': 'django_redis.client.DefaultClient',
#                 'COMPRESSOR': 'django_redis.compressors.zlib.ZlibCompressor',
#             }
#         }
#     }

# Cache des sessions pour performance
SESSION_ENGINE = 'django.contrib.sessions.backends.cached_db'

# Optimisation des requêtes DB
DATA_UPLOAD_MAX_NUMBER_FIELDS = 10000  # Pour les gros formulaires

# Template caching (activé automatiquement en production via loaders ci-dessus)

# Prefetch/Select related batch size
PREFETCH_BATCH_SIZE = 1000


# ── Content Security Policy ──
CSP_DEFAULT_SRC = ("'self'",)
CSP_SCRIPT_SRC = ("'self'", "'unsafe-inline'", "cdn.jsdelivr.net", "cdnjs.cloudflare.com")
CSP_STYLE_SRC = ("'self'", "'unsafe-inline'", "cdn.jsdelivr.net", "cdnjs.cloudflare.com", "fonts.googleapis.com")
CSP_FONT_SRC = ("'self'", "fonts.gstatic.com", "cdn.jsdelivr.net")
CSP_IMG_SRC = ("'self'", "data:", "blob:")
CSP_CONNECT_SRC = ("'self'",)
CSP_FRAME_ANCESTORS = ("'none'",)
