# redeploy-marker: 2026-07-29T13-40 (temporary Atlas migration endpoint)
from fastapi import FastAPI, APIRouter, HTTPException, Depends, status, UploadFile, File, Form
from fastapi.responses import Response
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.staticfiles import StaticFiles
from dotenv import load_dotenv
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.gzip import GZipMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
import os
import logging
import shutil
import asyncio
import json
import re
import time
import urllib.request
import urllib.error
from pathlib import Path
from pydantic import BaseModel, Field, field_validator
from typing import List, Optional
import uuid
from datetime import datetime, timezone, timedelta
import jwt
import bcrypt

ROOT_DIR = Path(__file__).parent
UPLOAD_DIR = ROOT_DIR / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB

load_dotenv(ROOT_DIR / '.env')

# MongoDB connection
mongo_url = os.environ['MONGO_URL']
client = AsyncIOMotorClient(mongo_url)
db = client[os.environ['DB_NAME']]

# JWT Settings
SECRET_KEY = os.environ.get('JWT_SECRET', 'pav-secret-key-2026-minimum-32-chars!')
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_HOURS = 24

app = FastAPI(title="PAV Management System API")
SERVER_START_TIME = time.time()  # Used by /admin/system-status to report backend uptime
api_router = APIRouter(prefix="/api")
security = HTTPBearer()

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ==================== HEALTH CHECK ====================
@app.get("/health")
async def health_check():
    """Health check endpoint for Kubernetes"""
    return {"status": "healthy", "service": "pav-api"}

@api_router.get("/health")
async def api_health_check():
    """Health check endpoint via API prefix"""
    return {"status": "healthy", "service": "pav-api"}

# ==================== ENUMS ====================
NIVEAUX_TECHNICIEN = ["Novice", "Débutant", "Intermédiaire", "Confirmé", "Expert"]
NIVEAUX_ACCES = ["Technicien", "Gestionnaire", "Responsable", "Admin", "Super Admin"]  # "Technicien" = ex-"Membre" (renamed, same permissions)
BRANCHES = ["Supervision", "Coordination", "Production", "Live", "Animation", "Régisseurs", "Diffusion"]
SOUS_BRANCHES_LIVE = ["Incrustation", "Diffusion", "Cadreur", "Réalisation"]
# Canonical postes used to filter the Planning assignment dropdown to only
# the people actually qualified for a given role — independent of branche,
# so exceptions (e.g. someone outside branche Live who can still fill in as
# Réalisateur) are handled by simply ticking that poste on their fiche.
POSTES = [
    "Supervision Régie",
    "Réalisateur",
    "Assistant réalisateur / truquiste",
    "Étalonneur",
    "Opérateur VDO",
    "Opérateur Incrustation",
    "Animateur VDO / VFX",
    "Intercom / Enregistrement FCP",
    "Cadreur (Caméra)",
    "Supervision Régisseurs",
    "Régisseur",
    "Supervision Diffusion",
    "Diffusion (Salles)",
]
CATEGORIES_MATERIEL = ["Caméra", "Trépied", "Batterie", "Câble", "Câble HDMI", "Câble SDI", "Câble XLR", "Câble Ethernet", "Micro", "Son", "Lumière", "Moniteur", "Enregistreur", "Accessoire", "Autre"]
STATUTS_DEVIS = ["En attente", "Validé", "Refusé", "Archivé"]
STATUTS_FORMATION = ["En attente Coordination", "En attente validation finale", "Validée", "Refusée", "Archivée"]
STATUTS_MATERIEL = ["Disponible", "En utilisation", "En maintenance", "Hors service", "Archivé"]
STATUTS_RESERVATION = ["En attente", "Validée", "Refusée", "Annulée"]
COMPETENCES_FORMATION = [
    'Réalisation', 'Cadreur', 'Opérateur VDO', 'Opérateur Incrustation', 'Animateur VFX',
    'Étalonneur', 'Assistant Réalisateur', 'Intercom / FCP', 'Supervision Régie', 'Régisseur',
    'Animation', 'Post-Production', 'Logistique Technique', 'Son', 'Éclairage', 'Autre'
]

# Permissions list for groups
PERMISSIONS = [
    "effectif.read", "effectif.write", "effectif.delete",
    "planning.read", "planning.write", "planning.delete",
    "logistique.read", "logistique.write", "logistique.delete",
    "devis.read", "devis.write", "devis.validate", "devis.delete",
    "formations.read", "formations.write", "formations.validate", "formations.delete",
    "salles.read", "salles.write", "salles.reservations", "salles.delete",
    "admin.users", "admin.groups", "admin.logs"
]

# ==================== MODELS ====================

class UserCreate(BaseModel):
    username: str
    password: str
    full_name: str
    niveau_acces: str
    branches: Optional[List[str]] = []  # Scopes Dashboard/Effectif visibility for Gestionnaire/Responsable. Empty = unrestricted.

class UserUpdate(BaseModel):
    full_name: Optional[str] = None
    niveau_acces: Optional[str] = None
    branches: Optional[List[str]] = None

class UserLogin(BaseModel):
    username: str
    password: str

class PasswordChange(BaseModel):
    new_password: str

class UserResponse(BaseModel):
    id: str
    username: str
    full_name: str
    niveau_acces: str
    branches: Optional[List[str]] = []
    technicien_id: Optional[str] = None
    created_at: str
    is_active: bool
    must_change_password: Optional[bool] = False
    # Missing on users created before this field existed -> treated as
    # "already seen" (True) so the onboarding guide only pops up for genuinely
    # new accounts, not retroactively for the whole existing team.
    onboarding_seen: Optional[bool] = True
    # Badge — request/renewal workflow. None | "en_attente_validation" | "non_conforme" | "validee"
    badge_status: Optional[str] = None
    badge_photo_url: Optional[str] = None
    badge_is_renewal: Optional[bool] = False
    badge_requested_at: Optional[str] = None
    badge_reviewed_at: Optional[str] = None
    badge_reviewed_by_name: Optional[str] = None
    badge_message: Optional[str] = None
    badge_motif: Optional[str] = None

class RegisterRequest(BaseModel):
    technicien_id: str
    username: str
    password: str

class AbsenceCreate(BaseModel):
    date_debut: str  # YYYY-MM-DD
    date_fin: str    # YYYY-MM-DD
    raison: str

class AbsenceResponse(BaseModel):
    id: str
    user_id: str
    full_name: str
    date_debut: str
    date_fin: str
    raison: str
    created_at: str

class TokenResponse(BaseModel):
    access_token: str
    token_type: str
    user: UserResponse

class TechnicienCreate(BaseModel):
    nom: str
    prenom: Optional[str] = ""  # Kept for backward compatibility; the UI now uses a single "Nom" field
    niveau_technicien: Optional[str] = None  # Not mandatory — some techniciens don't have a level assigned yet
    niveau_acces: str
    branches: List[str]  # Support multiple branches
    sous_branches: Optional[List[str]] = []  # Support multiple sous-branches (Live only)
    badge_attribue: bool = False
    telephone: Optional[str] = None
    email: Optional[str] = None
    # Poste(s) de prédilection — used to filter the Planning assignment
    # dropdown to only relevant people. Independent of branches, so
    # exceptions (someone doing a poste outside their usual branche) are
    # just an extra tick here.
    poste_principal: Optional[str] = None
    postes_secondaires: Optional[List[str]] = []

class TechnicienResponse(BaseModel):
    id: str
    nom: str
    prenom: str
    niveau_technicien: Optional[str] = None
    niveau_acces: str
    branches: List[str]  # Support multiple branches
    sous_branches: Optional[List[str]] = []
    badge_attribue: bool
    telephone: Optional[str] = None
    email: Optional[str] = None
    poste_principal: Optional[str] = None
    postes_secondaires: Optional[List[str]] = []
    is_archived: bool
    created_at: str
    updated_at: str
    # Fiches créées par un Responsable passent par une validation Coordination
    # avant d'intégrer définitivement l'effectif (workflow de soumission).
    is_pending_approval: Optional[bool] = False
    proposed_by: Optional[str] = None
    proposed_by_name: Optional[str] = None
    rejection_reason: Optional[str] = None

class TechnicienRejectRequest(BaseModel):
    message: Optional[str] = None

class BadgeRejectRequest(BaseModel):
    message: str

class MaterielCreate(BaseModel):
    nom: str
    categorie: str
    quantite: int = 1
    numero_serie: Optional[str] = None
    marque: Optional[str] = None
    modele: Optional[str] = None
    statut: str = "Disponible"
    notes: Optional[str] = None

class MaterielResponse(BaseModel):
    id: str
    nom: str
    categorie: str
    quantite: int = 1
    numero_serie: Optional[str] = None
    marque: Optional[str] = None
    modele: Optional[str] = None
    statut: str
    notes: Optional[str] = None
    is_archived: bool
    created_at: str
    updated_at: str

class FournisseurCreate(BaseModel):
    nom: str
    contact: str
    email: str
    telephone: str
    adresse: str
    categorie: str

class FournisseurResponse(BaseModel):
    id: str
    nom: str
    contact: str
    email: str
    telephone: str
    adresse: str
    categorie: str
    statut: str
    is_archived: bool
    created_at: str
    updated_at: str

class DevisCreate(BaseModel):
    titre: str
    fournisseur_id: Optional[str] = None
    montant: float
    description: str
    evenement: Optional[str] = None

class DevisResponse(BaseModel):
    id: str
    titre: str
    fournisseur_id: Optional[str] = None
    fournisseur_nom: Optional[str] = None
    montant: float
    description: str
    evenement: Optional[str] = None
    statut: str
    created_by: str
    created_by_name: str
    created_at: str
    validated_by: Optional[str] = None
    validated_at: Optional[str] = None
    is_archived: bool

class FormationCreate(BaseModel):
    titre: str
    description: str
    dates_souhaitees: List[str] = []  # multiple, non-contiguous days allowed
    duree: str

class FormationCoordinationValidate(BaseModel):
    formateur: str
    cursus: str
    lieu: str
    duree: Optional[str] = None  # Coordination may confirm/adjust the requested duration

class FormationRejectReason(BaseModel):
    motif: Optional[str] = None

class FormationResponse(BaseModel):
    id: str
    titre: str
    description: str
    dates_souhaitees: List[str] = []
    duree: str
    formateur: Optional[str] = None
    cursus: Optional[str] = None
    lieu: Optional[str] = None
    statut: str
    created_by: str
    created_by_name: str
    created_at: str
    coordination_by: Optional[str] = None
    coordination_at: Optional[str] = None
    validated_by: Optional[str] = None
    validated_at: Optional[str] = None
    motif_refus: Optional[str] = None
    refused_stage: Optional[str] = None  # "coordination" | "direction"
    is_archived: bool
    archived_at: Optional[str] = None
    disponible_catalogue: Optional[bool] = False
    origine: Optional[str] = "demande_membre"  # "demande_membre" | "proposition_responsable" | "suggestion_membre"
    interested_members: List[str] = []

class FormationCatalogueToggle(BaseModel):
    disponible_catalogue: bool

# Formation suggestions — a lightweight channel (distinct from the full
# Coordination -> Direction request workflow above) that lets ANY user,
# including Membre, suggest a new formation topic or express interest in
# an existing catalogue formation. Coordination/Admin review these.
class FormationSuggestionCreate(BaseModel):
    titre: str
    description: Optional[str] = ""
    formation_id: Optional[str] = None  # set when it's "interest" in an existing catalogue item

class FormationSuggestionStatusUpdate(BaseModel):
    statut: str  # "Approuvée" | "Rejetée"

class FormationSuggestionResponse(BaseModel):
    id: str
    titre: str
    description: Optional[str] = ""
    formation_id: Optional[str] = None
    statut: str
    created_by: str
    created_by_name: str
    created_at: str
    resulting_formation_id: Optional[str] = None
    is_archived: bool = False
    archived_at: Optional[str] = None

# Actualités models
class ActualiteCreate(BaseModel):
    titre: str
    description: Optional[str] = None
    date_evenement: Optional[str] = None
    image_url: Optional[str] = None

class ActualiteResponse(BaseModel):
    id: str
    titre: str
    description: Optional[str] = None
    date_evenement: Optional[str] = None
    image_url: Optional[str] = None
    created_by: str
    created_by_name: str
    created_at: str
    is_active: bool

# Documents models
class DocumentCategoryCreate(BaseModel):
    nom: str
    description: Optional[str] = None

class DocumentCategoryResponse(BaseModel):
    id: str
    nom: str
    description: Optional[str] = None
    created_at: str

class DocumentCreate(BaseModel):
    titre: str
    categorie_id: str
    description: Optional[str] = None
    file_url: str
    file_type: str  # pdf, png, jpg, etc.
    # Segmentation : quels niveaux d'accès peuvent voir ce document. Vide/absent
    # = visible par tout le monde (comportement historique, rétrocompatible
    # avec les documents existants qui n'ont pas ce champ). Une liste non vide
    # restreint la visibilité à ces niveaux uniquement — typiquement pour
    # exclure "Technicien" des documents internes à la gestion.
    visible_roles: Optional[List[str]] = None

class DocumentResponse(BaseModel):
    id: str
    titre: str
    categorie_id: str
    categorie_nom: Optional[str] = None
    description: Optional[str] = None
    file_url: str
    file_type: str
    created_by: str
    created_by_name: str
    created_at: str
    visible_roles: Optional[List[str]] = None

class PlanningCreate(BaseModel):
    mois: int
    annee: int
    dates: dict  # Changed to dict: {dimanche: [], vendredi: []}
    affectations: dict
    sections: Optional[dict] = None
    notes: Optional[dict] = None
    absences: Optional[dict] = None
    blocked_cells: Optional[dict] = None
    # Titre/sous-titre personnalisés par jour, ex: {"dimanche": {"titre": "...", "sous_titre": "..."}}.
    # Absent ou vide -> l'app retombe sur le titre calculé automatiquement (mois/année/jour).
    titre_overrides: Optional[dict] = None
    # Texte libre affiché sous une date précise (ex: nom d'invité, "Fête des
    # pères"), ex: {"dimanche": {"2026-06-14": "Invité : Jonathan Stockstill"}}.
    date_labels: Optional[dict] = None

class PlanningResponse(BaseModel):
    id: str
    mois: int
    annee: int
    dates: dict
    affectations: dict
    sections: Optional[dict] = None
    notes: Optional[dict] = None
    absences: Optional[dict] = None
    blocked_cells: Optional[dict] = None
    titre_overrides: Optional[dict] = None
    date_labels: Optional[dict] = None
    is_archived: bool
    created_at: str
    updated_at: str

class ResetPasswordRequest(BaseModel):
    new_password: str

class GroupCreate(BaseModel):
    name: str
    permissions: List[str]

class GroupResponse(BaseModel):
    id: str
    name: str
    permissions: List[str]
    created_at: str

class LogResponse(BaseModel):
    id: str
    action: str
    user_id: str
    user_name: str
    details: str
    timestamp: str

# ==================== SALLES MODELS ====================

class SalleCreate(BaseModel):
    nom: str
    capacite: Optional[int] = None
    equipements: Optional[str] = None
    description: Optional[str] = None

class SalleResponse(BaseModel):
    id: str
    nom: str
    capacite: Optional[int] = None
    equipements: Optional[str] = None
    description: Optional[str] = None
    is_archived: bool
    created_at: str

class CreneauCreate(BaseModel):
    nom: str  # e.g., "Matin", "Après-midi", "Soir"
    heure_debut: str  # e.g., "08:00"
    heure_fin: str  # e.g., "12:00"

class CreneauResponse(BaseModel):
    id: str
    nom: str
    heure_debut: str
    heure_fin: str
    is_active: bool
    created_at: str

class ShareLinkCreate(BaseModel):
    nom: str  # Description du lien
    duree_heures: int  # Durée de vie en heures
    mot_de_passe: Optional[str] = None
    salles_ids: List[str]  # Salles accessibles via ce lien

class ShareLinkResponse(BaseModel):
    id: str
    nom: str
    token: str  # Token unique pour le lien
    expires_at: str
    has_password: bool
    salles_ids: List[str]
    created_by: str
    created_by_name: str
    created_at: str
    is_active: bool

class ReservationCreate(BaseModel):
    salle_id: str
    date: str  # YYYY-MM-DD
    creneau_id: str
    nom_demandeur: str
    telephone: str
    email: str
    raison: str

    @field_validator('email')
    @classmethod
    def validate_email_format(cls, v):
        v = (v or '').strip()
        if not re.match(r'^[^@\s]+@[^@\s]+\.[^@\s]+$', v):
            raise ValueError("Adresse email invalide")
        return v

    @field_validator('telephone')
    @classmethod
    def validate_telephone_format(cls, v):
        v = (v or '').strip()
        digits = re.sub(r'[^0-9]', '', v)
        if len(digits) < 8 or len(digits) > 15 or not re.match(r'^[0-9+\s().-]+$', v):
            raise ValueError("Numéro de téléphone invalide")
        return v

class ReservationResponse(BaseModel):
    id: str
    salle_id: str
    salle_nom: str
    date: str
    creneau_id: str
    creneau_nom: str
    heure_debut: str
    heure_fin: str
    nom_demandeur: str
    telephone: str
    email: str
    raison: str
    statut: str
    raison_refus: Optional[str] = None
    validated_by: Optional[str] = None
    validated_at: Optional[str] = None
    created_at: str
    created_by_admin: Optional[bool] = False

class ShareLinkAccess(BaseModel):
    mot_de_passe: Optional[str] = None

class RejectReservationRequest(BaseModel):
    raison_refus: str

class AdminReservationCreate(BaseModel):
    salle_id: str
    date: str  # YYYY-MM-DD
    creneau_id: str
    nom_demandeur: str
    telephone: Optional[str] = ""
    email: Optional[str] = ""
    raison: str
    statut: str = "Validée"  # Admin can pre-validate

# ==================== MAINTENANCE MODE MODELS ====================

class MaintenanceModeUpdate(BaseModel):
    is_active: bool
    message: Optional[str] = None
    scope: Optional[str] = "site"  # "site" = whole app, "page" = a single page path
    page_path: Optional[str] = None  # required when scope == "page"
    # Roles impacted by this maintenance activation. None/empty = everyone below Super Admin
    # (default/legacy behavior). Super Admin is never impacted regardless of this list.
    affected_roles: Optional[List[str]] = None

class MaintenanceModeResponse(BaseModel):
    is_active: bool
    message: Optional[str] = None
    activated_by: Optional[str] = None
    activated_at: Optional[str] = None
    scope: Optional[str] = "site"
    page_path: Optional[str] = None
    affected_roles: Optional[List[str]] = None

# ==================== GROUP MODELS (ENHANCED) ====================

class GroupCreateEnhanced(BaseModel):
    name: str
    description: Optional[str] = None
    permissions: List[str]
    # Planning access scoping. Only meaningful for the "Responsable" role —
    # Gestionnaire+ already get unrestricted planning.write via the role
    # hierarchy in check_access_or_permission. planning_full_control=True
    # grants a Responsable the same unrestricted grid access as Gestionnaire+.
    # planning_scope is a list of Planning section names (e.g. "CADREURS",
    # "REGIE", "DIFFUSION", "REGISSEURS") and/or role-key substrings (e.g.
    # "incrustation", "animateur_vfx") a scoped Responsable may fill in.
    planning_full_control: bool = False
    planning_scope: List[str] = []

class GroupResponseEnhanced(BaseModel):
    id: str
    name: str
    description: Optional[str] = None
    permissions: List[str]
    planning_full_control: bool = False
    planning_scope: List[str] = []
    members_count: int
    created_at: str
    updated_at: str

class UserGroupAssignment(BaseModel):
    user_id: str
    group_ids: List[str]

# ==================== HELPER FUNCTIONS ====================

def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')

def verify_password(password: str, hashed: str) -> bool:
    return bcrypt.checkpw(password.encode('utf-8'), hashed.encode('utf-8'))

def create_token(user_id: str, username: str, niveau_acces: str) -> str:
    expire = datetime.now(timezone.utc) + timedelta(hours=ACCESS_TOKEN_EXPIRE_HOURS)
    payload = {"sub": user_id, "username": username, "niveau_acces": niveau_acces, "exp": expire}
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)

async def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(security)):
    try:
        payload = jwt.decode(credentials.credentials, SECRET_KEY, algorithms=[ALGORITHM])
        user_id = payload.get("sub")
        if user_id is None:
            raise HTTPException(status_code=401, detail="Token invalide")
        user = await db.users.find_one({"id": user_id}, {"_id": 0})
        if not user:
            raise HTTPException(status_code=401, detail="Utilisateur non trouvé")
        return user
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expiré")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Token invalide")

def check_access(user: dict, required_levels: List[str]):
    if user['niveau_acces'] not in required_levels:
        raise HTTPException(status_code=403, detail="Accès non autorisé")

# ---- Compte propriétaire & compte de secours protégés ----
# "Guichard" est le compte historique du titulaire de l'application.
# "Guichard_Secours" est un second compte Super Admin caché (voir
# hidden_account plus bas + filtrage dans GET /auth/users), qui sert de
# filet de sécurité si jamais l'accès au compte principal était perdu.
# Ni l'un ni l'autre ne peut être supprimé, désactivé ou rétrogradé par un
# autre Super Admin — ce sont les seuls comptes de l'application avec cette
# garantie, précisément parce qu'ils ne dépendent d'aucun autre compte pour
# être restaurés en cas de problème.
PROTECTED_USERNAMES = {"Guichard", "Guichard_Secours"}

def is_owner_account(user: dict) -> bool:
    return bool(user) and user.get('username') == 'Guichard'

def is_protected_account(user: dict) -> bool:
    return bool(user) and user.get('username') in PROTECTED_USERNAMES

def assert_account_not_protected(target_user: dict):
    if is_protected_account(target_user):
        raise HTTPException(status_code=403, detail="Ce compte est protégé (compte titulaire ou compte de secours) et ne peut pas être modifié depuis cette action.")

async def get_user_group_permissions(user: dict) -> set:
    """Union of permissions granted via every Groupe (Administration > Groupes
    & Droits) this user belongs to. Empty set if they're in no group."""
    group_ids = user.get('group_ids') or []
    if not group_ids:
        return set()
    groups = await db.groups.find({"id": {"$in": group_ids}}, {"_id": 0, "permissions": 1}).to_list(100)
    perms: set = set()
    for g in groups:
        perms.update(g.get('permissions') or [])
    return perms

async def get_user_planning_scope(user: dict):
    """Planning scoping derived from group membership — only meaningful for
    the Responsable role (Gestionnaire+ already bypass restriction via role).
    Returns (full_control: bool, scope: set[str]) where scope entries are
    either Planning section names ("CADREURS", "REGIE", ...) or role-key
    substrings ("incrustation", "animateur_vfx", ...)."""
    group_ids = user.get('group_ids') or []
    if not group_ids:
        return False, set()
    groups = await db.groups.find({"id": {"$in": group_ids}}, {"_id": 0}).to_list(100)
    full_control = any(g.get('planning_full_control') for g in groups)
    scope: set = set()
    for g in groups:
        scope.update(g.get('planning_scope') or [])
    return full_control, scope

def build_role_section_map(sections_dict: dict) -> dict:
    """Flatten a Planning `sections` document into
    {affectation_key: section_name}, where affectation_key mirrors the
    frontend's `${role.key}_${slotIdx}` convention. Used to check which
    section a given affectation edit belongs to."""
    mapping: dict = {}
    if not isinstance(sections_dict, dict):
        return mapping
    for _day, tables in sections_dict.items():
        if not isinstance(tables, dict):
            continue
        for _table_key, secs in tables.items():
            if not isinstance(secs, list):
                continue
            for section in secs:
                name = section.get('name', '') if isinstance(section, dict) else ''
                for role in (section.get('roles', []) if isinstance(section, dict) else []):
                    role_key = role.get('key') if isinstance(role, dict) else None
                    slots = role.get('slots', 1) if isinstance(role, dict) else 1
                    if not role_key:
                        continue
                    for slot_idx in range(slots):
                        mapping[f"{role_key}_{slot_idx}"] = name
    return mapping

async def check_access_or_permission(user: dict, required_levels: List[str], permission: str):
    """Same baseline as check_access (role hierarchy), OR'd with an explicit
    permission grant from a Groupe, OR'd with an explicit grant from the
    role-permissions matrix (Administration > Droits d'accès). Super Admin
    always passes; Admin is NOT auto-bypassed here anymore — its access to
    every one of these permissions is a real, revocable grant (seeded once
    at startup to preserve pre-existing behaviour, see seed_admin_permissions)."""
    if user['niveau_acces'] == "Super Admin" or user['niveau_acces'] in required_levels:
        return
    perms = await get_user_group_permissions(user)
    if permission in perms:
        return
    if await role_has_permission(user['niveau_acces'], permission):
        return
    raise HTTPException(status_code=403, detail="Accès non autorisé")

# ==================== MATRICE DES DROITS PAR RÔLE ====================
# Configurable depuis Administration > Droits d'accès. Chaque entrée décrit
# un droit précis et, pour chacun des 3 rôles configurables, s'il est déjà
# "de base" (verrouillé, non modifiable ici), interdit (verrouillé à false,
# séparation des tâches), ou librement activable/désactivable par un Super
# Admin. Le rôle Super Admin a toujours tous les droits et n'apparaît pas
# comme colonne éditable.
#
# admin_default=True signifie que le droit fait partie du comportement
# historique d'Admin (accès total sauf Supervision) et a été accordé une
# fois pour toutes au démarrage (voir seed_admin_permissions) — Admin reste
# libre d'être décoché ensuite par un Super Admin, ce n'est plus verrouillé.
CONFIGURABLE_ROLES = ["Responsable", "Gestionnaire", "Admin"]

PERMISSION_CATALOG = [
    # --- Actualités ---
    {"key": "actualites.write", "label": "Actualités — publier / modifier", "baseline": ["Gestionnaire", "Responsable"], "admin_default": True},
    # --- Documents ---
    {"key": "documents.write", "label": "Documents — ajouter / modifier (base de connaissance)", "baseline": ["Gestionnaire", "Responsable"], "admin_default": True},
    # --- Effectif ---
    {"key": "effectif.write", "label": "Effectif — créer / modifier une fiche technicien", "baseline": ["Responsable"], "admin_default": True},
    {"key": "effectif.approve", "label": "Effectif — valider / refuser une fiche proposée par un Responsable", "baseline": ["Gestionnaire"], "admin_default": True, "forbidden": ["Responsable"]},
    {"key": "effectif.delete", "label": "Effectif — archiver une fiche technicien", "baseline": [], "admin_default": True},
    # --- Planning ---
    {"key": "planning.write", "label": "Planning — modifier les affectations (le détail par section reste géré via Groupes & Droits)", "baseline": ["Gestionnaire", "Responsable"], "admin_default": True},
    {"key": "planning.delete", "label": "Planning — archiver un planning", "baseline": [], "admin_default": True},
    # --- Devis ---
    {"key": "devis.validate", "label": "Devis — valider / refuser un devis", "baseline": ["Responsable"], "admin_default": True},
    {"key": "devis.delete", "label": "Devis — archiver un devis", "baseline": [], "admin_default": True},
    # --- Formations ---
    {"key": "formations.validate", "label": "Formations — valider une demande", "baseline": ["Gestionnaire", "Responsable"], "admin_default": True},
    {"key": "formations.write", "label": "Formations — gérer les suggestions", "baseline": ["Gestionnaire", "Responsable"], "admin_default": True},
    {"key": "formations.delete", "label": "Formations — archiver une formation", "baseline": [], "admin_default": True},
    # --- Logistique / Matériel ---
    {"key": "logistique.write", "label": "Logistique — créer / modifier du matériel", "baseline": ["Gestionnaire", "Responsable"], "admin_default": True},
    {"key": "logistique.delete", "label": "Logistique — archiver du matériel", "baseline": [], "admin_default": True},
    # --- Salles ---
    {"key": "salles.write", "label": "Salles — gérer les salles et créneaux", "baseline": [], "admin_default": True},
    {"key": "salles.reservations", "label": "Salles — valider / refuser une réservation", "baseline": ["Responsable"], "admin_default": True},
    # --- Administration / Supervision (Admin non accordé par défaut) ---
    {"key": "admin.restart", "label": "Supervision — redémarrer le serveur", "baseline": [], "admin_default": False},
    {"key": "admin.cleanup", "label": "Supervision — nettoyer les fichiers orphelins / purger les logs", "baseline": [], "admin_default": False},
    {"key": "admin.storage_quota", "label": "Supervision — modifier le quota de stockage", "baseline": [], "admin_default": False},
    {"key": "admin.maintenance", "label": "Maintenance — activer / désactiver le mode maintenance", "baseline": [], "admin_default": False},
]
_PERMISSION_CATALOG_BY_KEY = {e["key"]: e for e in PERMISSION_CATALOG}

async def role_has_permission(role: str, permission: str) -> bool:
    """Explicit grant made via Administration > Droits d'accès for a role
    that is neither baseline nor forbidden for this permission."""
    doc = await db.role_permissions.find_one({"role": role, "permission": permission}, {"_id": 0})
    return doc is not None

async def check_access_or_role_permission(user: dict, required_levels: List[str], permission: str):
    """Strict role check (no Admin bypass), OR'd with an explicit grant from
    the role-permissions matrix. Used for sensitive Supervision/Maintenance
    actions where Admin must NOT have access unless a Super Admin explicitly
    ticks the box in Administration > Droits d'accès."""
    if user['niveau_acces'] in required_levels:
        return
    if await role_has_permission(user['niveau_acces'], permission):
        return
    raise HTTPException(status_code=403, detail="Accès non autorisé")

class RolePermissionUpdate(BaseModel):
    role: str
    permission: str
    granted: bool

@api_router.get("/admin/role-permissions")
async def get_role_permissions_matrix(current_user: dict = Depends(get_current_user)):
    check_access(current_user, ["Super Admin"])
    docs = await db.role_permissions.find({}, {"_id": 0}).to_list(1000)
    grants: dict = {}
    for d in docs:
        grants.setdefault(d['role'], set()).add(d['permission'])
    rows = []
    for entry in PERMISSION_CATALOG:
        row = {"key": entry["key"], "label": entry["label"], "roles": {}}
        for role in CONFIGURABLE_ROLES:
            if role in entry.get("forbidden", []):
                row["roles"][role] = {"granted": False, "locked": True, "reason": "Non autorisable pour ce rôle (séparation des tâches)"}
            elif role in entry["baseline"]:
                row["roles"][role] = {"granted": True, "locked": True, "reason": "Droit de base du rôle"}
            else:
                row["roles"][role] = {"granted": entry["key"] in grants.get(role, set()), "locked": False, "reason": None}
        rows.append(row)
    return {"rows": rows}

@api_router.put("/admin/role-permissions")
async def update_role_permission(data: RolePermissionUpdate, current_user: dict = Depends(get_current_user)):
    check_access(current_user, ["Super Admin"])
    if data.role not in CONFIGURABLE_ROLES:
        raise HTTPException(status_code=400, detail="Rôle invalide")
    entry = _PERMISSION_CATALOG_BY_KEY.get(data.permission)
    if not entry:
        raise HTTPException(status_code=404, detail="Droit inconnu")
    if data.role in entry["baseline"] or data.role in entry.get("forbidden", []):
        raise HTTPException(status_code=400, detail="Ce droit ne peut pas être modifié pour ce rôle")
    if data.granted:
        await db.role_permissions.update_one(
            {"role": data.role, "permission": data.permission},
            {"$set": {"role": data.role, "permission": data.permission}},
            upsert=True,
        )
    else:
        await db.role_permissions.delete_one({"role": data.role, "permission": data.permission})
    await log_action(
        current_user['id'], current_user['full_name'], "Modification droits d'accès",
        f"{data.role} / {entry['label']} -> {'accordé' if data.granted else 'retiré'}"
    )
    return {"message": "Droit mis à jour"}

def is_coordination_or_admin(user: dict) -> bool:
    """Coordination step of the Formations workflow: Gestionnaire+ in the
    Coordination branch, or Admin/Super Admin (who retain full visibility)."""
    if user['niveau_acces'] in ("Admin", "Super Admin"):
        return True
    if user['niveau_acces'] in ("Gestionnaire", "Responsable", "Coordination") and "Coordination" in (user.get("branches") or []):
        return True
    return False

def is_direction_or_admin(user: dict) -> bool:
    """Final validation step of the Formations workflow: department-wide
    Responsables (branches empty = unrestricted oversight, e.g. Paul), or
    Admin/Super Admin."""
    if user['niveau_acces'] in ("Admin", "Super Admin"):
        return True
    if user['niveau_acces'] == "Responsable" and not (user.get("branches") or []):
        return True
    return False

# ==================== IN-APP NOTIFICATIONS ====================
# Lightweight per-recipient notification docs (one doc per recipient, so
# read/unread is trivially per-user) surfaced via a bell icon in the topbar.
# Complements the existing email alerts — some events (RGPD deletion
# requests, absence declarations) previously only reached people by email
# with no in-app trace once the email was missed/archived.

class NotificationResponse(BaseModel):
    id: str
    type: str
    titre: str
    message: str
    link: Optional[str] = None
    created_at: str
    is_read: bool = False
    read_at: Optional[str] = None

async def get_user_ids_by_roles(roles: List[str]) -> List[str]:
    users = await db.users.find({"niveau_acces": {"$in": roles}}, {"_id": 0, "id": 1}).to_list(1000)
    return [u["id"] for u in users]

async def get_coordination_user_ids() -> List[str]:
    """Same population as is_coordination_or_admin: Admin/Super Admin plus
    Gestionnaire/Responsable scoped to the Coordination branch."""
    admins = await get_user_ids_by_roles(["Admin", "Super Admin"])
    coord = await db.users.find(
        {"niveau_acces": {"$in": ["Gestionnaire", "Responsable"]}, "branches": "Coordination"},
        {"_id": 0, "id": 1}
    ).to_list(1000)
    return list(set(admins + [u["id"] for u in coord]))

async def create_notification(recipient_ids: List[str], type_: str, titre: str, message: str, link: Optional[str] = None):
    recipient_ids = [r for r in set(recipient_ids) if r]
    if not recipient_ids:
        return
    now = datetime.now(timezone.utc).isoformat()
    docs = [{
        "id": str(uuid.uuid4()),
        "recipient_id": rid,
        "type": type_,
        "titre": titre,
        "message": message,
        "link": link,
        "created_at": now,
        "is_read": False,
        "read_at": None,
    } for rid in recipient_ids]
    await db.notifications.insert_many(docs)

@api_router.get("/notifications", response_model=List[NotificationResponse])
async def get_my_notifications(current_user: dict = Depends(get_current_user)):
    notifs = await db.notifications.find({"recipient_id": current_user['id']}, {"_id": 0}).sort("created_at", -1).to_list(100)
    return [NotificationResponse(**n) for n in notifs]

@api_router.put("/notifications/{notification_id}/read")
async def mark_notification_read(notification_id: str, current_user: dict = Depends(get_current_user)):
    result = await db.notifications.update_one(
        {"id": notification_id, "recipient_id": current_user['id']},
        {"$set": {"is_read": True, "read_at": datetime.now(timezone.utc).isoformat()}}
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Notification non trouvée")
    return {"message": "Notification marquée comme lue"}

@api_router.put("/notifications/read-all")
async def mark_all_notifications_read(current_user: dict = Depends(get_current_user)):
    await db.notifications.update_many(
        {"recipient_id": current_user['id'], "is_read": False},
        {"$set": {"is_read": True, "read_at": datetime.now(timezone.utc).isoformat()}}
    )
    return {"message": "Toutes les notifications marquées comme lues"}

@api_router.delete("/notifications/{notification_id}")
async def delete_notification(notification_id: str, current_user: dict = Depends(get_current_user)):
    await db.notifications.delete_one({"id": notification_id, "recipient_id": current_user['id']})
    return {"message": "Notification supprimée"}

# ==================== EMAIL NOTIFICATIONS ====================
# Sent via the SendGrid HTTPS Email API (v3 /mail/send), not SMTP. Railway
# blocks all outbound SMTP on Free/Trial/Hobby plans regardless of provider,
# port, or IP version (see docs.railway.com/networking/outbound-networking) —
# this was discovered after the original Gmail-SMTP implementation silently
# failed on every send ("Network is unreachable", then "timed out" once IPv4
# was forced). SendGrid's API runs over plain HTTPS (port 443), which Railway
# never blocks, so it works on any Railway plan.
#
# Configure via Railway environment variables:
#   SENDGRID_API_KEY   -> API key from SendGrid (Settings -> API Keys)
#   SENDGRID_FROM_EMAIL -> the address emails are sent "from". Must be verified
#                          in SendGrid under Settings -> Sender Authentication ->
#                          Single Sender Verification (no domain/DNS required —
#                          just click the confirmation link SendGrid emails to
#                          that address). Currently pav.reservations@gmail.com.
#   EMAIL_FROM_NAME     -> display name for the From header (default "PAV Manager")
# If either SENDGRID_API_KEY or SENDGRID_FROM_EMAIL is unset, emails are
# silently skipped so the rest of the app keeps working.
#
# Note: sending "from" a Gmail address through a third-party API can be flagged
# or rejected by some receiving providers because of Gmail's own DMARC policy —
# this is a known deliverability trade-off of avoiding a custom domain, not a
# bug. Monitor the SendGrid Activity Feed if emails seem to go missing.
#
# Recipient routing: pav.resa (SENDGRID_FROM_EMAIL) is only the *sending*
# identity — a tunnel — not necessarily who reads the notifications. Real
# recipients are configured per domain so each team only gets what concerns it:
#   SALLES_NOTIFY_EMAIL         -> Salles/réservations (destinataire réel : Paul)
#   COORDINATION_NOTIFY_EMAIL   -> Devis + Formations   (destinataires réels : Delphine, Winchel)
#   ADMIN_NOTIFY_EMAIL          -> RGPD / administration générale (fallback des deux ci-dessus)
# All three currently point to the same test address (guichardelane1@gmail.com)
# until the real addresses for Paul/Delphine/Winchel are confirmed and set in
# Railway — see the two PAV Manager docx documents for the exact variable names.
SENDGRID_API_KEY = os.environ.get('SENDGRID_API_KEY')
SENDGRID_FROM_EMAIL = os.environ.get('SENDGRID_FROM_EMAIL') or os.environ.get('SMTP_USER')
EMAIL_FROM_NAME = os.environ.get('EMAIL_FROM_NAME', 'PAV Manager')
ADMIN_NOTIFY_EMAIL = os.environ.get('ADMIN_NOTIFY_EMAIL') or SENDGRID_FROM_EMAIL
SALLES_NOTIFY_EMAIL = os.environ.get('SALLES_NOTIFY_EMAIL') or ADMIN_NOTIFY_EMAIL
COORDINATION_NOTIFY_EMAIL = os.environ.get('COORDINATION_NOTIFY_EMAIL') or ADMIN_NOTIFY_EMAIL
EMAIL_ENABLED = bool(SENDGRID_API_KEY and SENDGRID_FROM_EMAIL)
APP_URL = os.environ.get('APP_URL', 'https://pav-manager-app.netlify.app')

# Optional infrastructure-monitoring credentials for Administration > Supervision
# (see /admin/infra-status below). Both are read-only "personal access token"
# style keys — neither can move money, delete data, or change billing on their
# own service, but treat them as secrets like any other API key here.
#   RENDER_API_KEY    -> Render dashboard > Account Settings > API Keys
#   RENDER_SERVICE_ID -> defaults to the current EU backend service if unset
#   NETLIFY_API_TOKEN -> Netlify dashboard > User settings > Applications > Personal access tokens
#   NETLIFY_SITE_ID   -> the site's "Site ID" (or its <name>.netlify.app subdomain), from Site settings > General
# Until these are set, /admin/infra-status still returns a live self-check of
# this backend + database, and reports the two hosting cards as "not configured"
# rather than failing.
RENDER_API_KEY = os.environ.get('RENDER_API_KEY')
RENDER_SERVICE_ID = os.environ.get('RENDER_SERVICE_ID', 'srv-d9l2oo2d0e5s73en37g0')
NETLIFY_API_TOKEN = os.environ.get('NETLIFY_API_TOKEN')
NETLIFY_SITE_ID = os.environ.get('NETLIFY_SITE_ID')

def _send_email_sync(to: str, subject: str, body_html: str):
    # A plain-text alternative alongside the HTML body. Multipart messages
    # (text/plain + text/html) are generally trusted more by spam filters
    # than HTML-only ones — one of the few levers left to help deliverability
    # without a custom verified sending domain (see comment above).
    text_body = re.sub(r'<br\s*/?>', '\n', body_html)
    text_body = re.sub(r'<[^>]+>', '', text_body)
    text_body = re.sub(r'\n{3,}', '\n\n', text_body).strip()
    payload = {
        "personalizations": [{"to": [{"email": to}]}],
        "from": {"email": SENDGRID_FROM_EMAIL, "name": EMAIL_FROM_NAME},
        "subject": subject,
        "content": [
            {"type": "text/plain", "value": text_body},
            {"type": "text/html", "value": body_html},
        ],
    }
    req = urllib.request.Request(
        "https://api.sendgrid.com/v3/mail/send",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {SENDGRID_API_KEY}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status not in (200, 202):
                raise RuntimeError(f"SendGrid a répondu avec le statut {resp.status}")
    except urllib.error.HTTPError as e:
        detail = e.read().decode('utf-8', errors='replace')
        raise RuntimeError(f"SendGrid HTTP {e.code}: {detail}") from e

async def notify_email(to: Optional[str], subject: str, body_html: str):
    """Best-effort transactional email, sent on a worker thread so the blocking
    HTTPS call to SendGrid never stalls the event loop. Never raises: a
    misconfigured API key or down SendGrid must not break the underlying
    workflow action (validation, refusal, etc.) — failures are only logged."""
    if not EMAIL_ENABLED or not to:
        return
    try:
        await asyncio.to_thread(_send_email_sync, to, subject, body_html)
    except Exception as e:
        logger.warning(f"Email non envoyé à {to} ({subject}): {e}")

# Fire-and-forget scheduling: awaiting notify_email() directly inside a request
# handler makes the HTTP response wait for the full SMTP round-trip (connect +
# STARTTLS + login + send to Gmail), which can take several seconds and made
# actions like "Valider"/"Refuser" feel slow. fire_and_forget_email() schedules
# the same notify_email() coroutine as a background asyncio Task instead, so the
# request returns immediately and the email is sent slightly after. A module-level
# set keeps a reference to each Task until it finishes (asyncio would otherwise be
# free to garbage-collect a Task with no other referent before it completes).
_background_email_tasks: set = set()

def fire_and_forget_email(to: Optional[str], subject: str, body_html: str):
    task = asyncio.create_task(notify_email(to, subject, body_html))
    _background_email_tasks.add(task)
    task.add_done_callback(_background_email_tasks.discard)

# Visual "kind" of a notification: controls the accent color and the small
# status badge shown under the title, so recipients can tell at a glance
# whether an email is a pending action, a confirmation, or a refusal.
EMAIL_KINDS = {
    "pending": {"accent": "#D97706", "bg": "#FFFBEB", "label": "EN ATTENTE"},
    "success": {"accent": "#059669", "bg": "#ECFDF5", "label": "VALIDÉ"},
    "danger":  {"accent": "#DC2626", "bg": "#FEF2F2", "label": "REFUSÉ"},
    "info":    {"accent": "#2563EB", "bg": "#EFF6FF", "label": "INFORMATION"},
}

def email_template(title: str, lines: List[str], kind: str = "info") -> str:
    style = EMAIL_KINDS.get(kind, EMAIL_KINDS["info"])
    rows = "".join(f"<p style='margin:0 0 10px 0;color:#374151;font-size:14px;line-height:1.5'>{l}</p>" for l in lines)
    return f"""
    <div style="font-family:Arial,Helvetica,sans-serif;max-width:540px;margin:0 auto;padding:0;border:1px solid #e5e7eb;border-radius:10px;overflow:hidden">
      <div style="background:#111827;padding:18px 24px;">
        <span style="font-size:17px;font-weight:bold;color:#ffffff;letter-spacing:0.5px">PAV MANAGER</span>
      </div>
      <div style="padding:24px">
        <span style="display:inline-block;background:{style['bg']};color:{style['accent']};font-size:11px;font-weight:bold;letter-spacing:0.5px;padding:4px 10px;border-radius:999px;margin-bottom:12px">{style['label']}</span>
        <h2 style="color:#111827;font-size:17px;margin:0 0 16px 0;line-height:1.4">{title}</h2>
        {rows}
        <a href="{APP_URL}" style="display:inline-block;margin-top:16px;background:{style['accent']};color:#ffffff;text-decoration:none;font-size:13px;font-weight:bold;padding:10px 18px;border-radius:6px">Ouvrir PAV Manager</a>
      </div>
      <div style="background:#F9FAFB;padding:14px 24px;border-top:1px solid #E5E7EB">
        <p style="margin:0;font-size:11px;color:#9CA3AF">Notification automatique du département Production Audiovisuelle — merci de ne pas répondre directement à cet email.</p>
      </div>
    </div>
    """

async def get_user_email(user_id: str) -> Optional[str]:
    """Login accounts (users) don't carry their own email — resolve it through
    the technicien profile they're linked to, if any."""
    user = await db.users.find_one({"id": user_id}, {"_id": 0})
    if not user or not user.get('technicien_id'):
        return None
    tech = await db.techniciens.find_one({"id": user['technicien_id']}, {"_id": 0})
    return tech.get('email') if tech else None

# ==================== EDITABLE SALLES EMAIL TEMPLATES ====================
# The wording of the 4 reservation-related emails can be customized from
# Salles > Notifications in the app (no code change/redeploy needed).
# Customizations live in the `email_templates` collection, keyed by these
# `key`s; anything not customized falls back to the defaults below. Body
# lines support simple {placeholder} substitution — see each entry's
# "placeholders" list for what's available in that context.
DEFAULT_EMAIL_TEMPLATES = {
    "reservation_recue": {
        "label": "Confirmation de réception (au demandeur)",
        "subject": "Demande de réservation reçue",
        "body_lines": [
            "Bonjour {nom_demandeur},",
            "<b>Salle :</b> {salle_nom}",
            "<b>Date :</b> {date}",
            "<b>Créneau :</b> {creneau_nom} ({heure_debut}-{heure_fin})",
            "Elle est en attente de validation — vous recevrez un email dès qu'elle sera traitée.",
        ],
        "kind": "pending",
        "placeholders": ["nom_demandeur", "salle_nom", "date", "creneau_nom", "heure_debut", "heure_fin", "raison"],
    },
    "reservation_a_valider": {
        "label": "Nouvelle demande à valider (destinataires internes)",
        "subject": "Nouvelle demande de réservation à valider",
        "body_lines": [
            "Bonjour,",
            "<b>Demandeur :</b> {nom_demandeur}",
            "<b>Salle :</b> {salle_nom}",
            "<b>Date :</b> {date}",
            "<b>Créneau :</b> {creneau_nom}",
            "<b>Raison :</b> {raison}",
        ],
        "kind": "pending",
        "placeholders": ["nom_demandeur", "salle_nom", "date", "creneau_nom", "heure_debut", "heure_fin", "raison"],
    },
    "reservation_validee": {
        "label": "Réservation validée (au demandeur + copie interne)",
        "subject": "Réservation validée",
        "body_lines": [
            "Bonjour {nom_demandeur},",
            "<b>Salle :</b> {salle_nom}",
            "<b>Date :</b> {date}",
            "<b>Créneau :</b> {creneau_nom} ({heure_debut}-{heure_fin})",
        ],
        "kind": "success",
        "placeholders": ["nom_demandeur", "salle_nom", "date", "creneau_nom", "heure_debut", "heure_fin"],
    },
    "reservation_refusee": {
        "label": "Réservation refusée (au demandeur)",
        "subject": "Réservation refusée",
        "body_lines": [
            "Bonjour {nom_demandeur},",
            "<b>Salle :</b> {salle_nom}",
            "<b>Date :</b> {date}",
            "<b>Raison du refus :</b> {raison_refus}",
        ],
        "kind": "danger",
        "placeholders": ["nom_demandeur", "salle_nom", "date", "raison_refus"],
    },
}

async def render_email_template(key: str, **kwargs):
    """Returns (subject, rendered_html) for a template key, applying any DB
    override and substituting {placeholders} from kwargs. Never raises on a
    missing placeholder — falls back to the raw (unsubstituted) text so a
    typo in a custom template can't break the underlying workflow action."""
    default = DEFAULT_EMAIL_TEMPLATES[key]
    stored = await db.email_templates.find_one({"key": key}, {"_id": 0})
    subject = (stored or {}).get("subject") or default["subject"]
    body_lines = (stored or {}).get("body_lines") or default["body_lines"]

    def safe_format(s: str) -> str:
        try:
            return s.format(**kwargs)
        except Exception:
            return s

    rendered_subject = safe_format(subject)
    rendered_lines = [safe_format(line) for line in body_lines]
    return rendered_subject, email_template(rendered_subject, rendered_lines, kind=default["kind"])

# ==================== CONFIGURABLE SALLES NOTIFICATION RECIPIENTS ====================
# Which internal address(es) get notified for each Salles case, editable from
# Salles > Notifications. Each recipient entry is either:
#   {"type": "technicien", "id": "..."} -> resolved to that technicien's
#       *current* email at send time (e.g. picking Paul here means his own
#       profile email is always used, even if he updates it later)
#   {"type": "email", "value": "..."}   -> a fixed address
# Falls back to SALLES_NOTIFY_EMAIL if nothing is configured for a case.
SALLES_NOTIFICATION_CASES = {
    "nouvelle_demande": "Nouvelle demande de réservation à valider",
    "confirmation": "Copie interne lors de la validation d'une réservation",
}

async def resolve_case_recipients(case: str) -> List[str]:
    settings = await db.notification_settings.find_one({"case": case}, {"_id": 0})
    recipients_config = (settings or {}).get("recipients", [])
    emails: List[str] = []
    for r in recipients_config:
        if r.get("type") == "technicien":
            tech = await db.techniciens.find_one({"id": r.get("id")}, {"_id": 0})
            if tech and tech.get("email"):
                emails.append(tech["email"])
        elif r.get("type") == "email" and r.get("value"):
            emails.append(r["value"])
    if not emails and SALLES_NOTIFY_EMAIL:
        emails = [SALLES_NOTIFY_EMAIL]
    seen = set()
    result = []
    for e in emails:
        if e and e not in seen:
            seen.add(e)
            result.append(e)
    return result

async def log_action(user_id: str, user_name: str, action: str, details: str):
    log_entry = {
        "id": str(uuid.uuid4()),
        "action": action,
        "user_id": user_id,
        "user_name": user_name,
        "details": details,
        "timestamp": datetime.now(timezone.utc).isoformat()
    }
    await db.logs.insert_one(log_entry)

# ==================== SEED DATA ====================

async def seed_data():
    # Migration: Rename Logistique to Régisseurs in techniciens
    await db.techniciens.update_many(
        {"branches": "Logistique"},
        {"$set": {"branches.$[elem]": "Régisseurs"}},
        array_filters=[{"elem": "Logistique"}]
    )
    await db.techniciens.update_many(
        {"branche": "Logistique"},
        {"$set": {"branche": "Régisseurs"}}
    )
    
    # Seed admin user
    admin = await db.users.find_one({"username": "Guichard"})
    if not admin:
        admin_user = {
            "id": str(uuid.uuid4()),
            "username": "Guichard",
            "password": hash_password("Telemarkhus_001!"),
            "full_name": "Guichard ELANE",
            "niveau_acces": "Super Admin",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "is_active": True
        }
        await db.users.insert_one(admin_user)
        logger.info("Admin user created: Guichard")

    # Compte de secours caché — filet de sécurité réservé à Guichard seul en
    # cas de perte d'accès au compte principal (voir PROTECTED_USERNAMES et
    # le filtrage "hidden_account" dans GET /auth/users). Créé une seule
    # fois ; le mot de passe initial n'est communiqué qu'à Guichard lui-même,
    # jamais affiché ni exposé par l'API ensuite — à changer dès la première
    # connexion.
    backup_admin = await db.users.find_one({"username": "Guichard_Secours"})
    if not backup_admin:
        backup_admin_user = {
            "id": str(uuid.uuid4()),
            "username": "Guichard_Secours",
            "password": hash_password("3l3kRY4pQYgdvKy9_60"),
            "full_name": "Compte de secours",
            "niveau_acces": "Super Admin",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "is_active": True,
            "hidden_account": True,
            "must_change_password": True,
        }
        await db.users.insert_one(backup_admin_user)
        logger.info("Hidden backup owner account created: Guichard_Secours")

    # ALWAYS update organigramme with the correct structure (migration)
    correct_organigramme = {
        "name": "PAV",
        "responsable": "Département Production Audiovisuelle",
        "children": [
            {
                "name": "RESP. PAV",
                "responsable": "Paul Baptista",
                "children": [
                    {"name": "COORDINATION", "responsable": "Delphine & Winchel", "children": []},
                    {"name": "PRODUCTION", "responsable": "Anne-Lise & Dylan", "children": []},
                    {
                        "name": "LIVE",
                        "responsable": "Christine, Nathalie & Jacob",
                        "children": [
                            {"name": "INCRUSTATION", "responsable": "Angelo", "children": []},
                            {"name": "DIFFUSION", "responsable": "Renaud", "children": []}
                        ]
                    },
                    {"name": "ANIMATION", "responsable": "Laura", "children": []},
                    {"name": "RÉGISSEURS", "responsable": "Joanna & Nicolas", "children": []}
                ]
            }
        ]
    }
    
    existing_org = await db.organigramme.find_one({})
    if existing_org:
        # Update existing organigramme
        await db.organigramme.update_one(
            {},
            {"$set": {"structure": correct_organigramme, "updated_at": datetime.now(timezone.utc).isoformat()}}
        )
        logger.info("Organigramme updated with correct structure")
    else:
        # Create new organigramme
        organigramme = {
            "id": str(uuid.uuid4()),
            "structure": correct_organigramme,
            "created_at": datetime.now(timezone.utc).isoformat()
        }
        await db.organigramme.insert_one(organigramme)
        logger.info("Organigramme seeded")

    # Seed some techniciens based on the planning image
    techniciens_count = await db.techniciens.count_documents({})
    if techniciens_count == 0:
        techniciens_data = [
            # Régie
            {"nom": "Christine", "prenom": "", "branches": ["Live"], "sous_branche": None, "niveau_technicien": "Expert", "niveau_acces": "Responsable"},
            {"nom": "Gabrielle", "prenom": "", "branches": ["Live"], "sous_branche": None, "niveau_technicien": "Confirmé", "niveau_acces": "Gestionnaire"},
            {"nom": "Nathalie", "prenom": "", "branches": ["Live"], "sous_branche": None, "niveau_technicien": "Expert", "niveau_acces": "Responsable"},
            {"nom": "Tiphaine", "prenom": "", "branches": ["Live"], "sous_branche": None, "niveau_technicien": "Confirmé", "niveau_acces": "Gestionnaire"},
            {"nom": "Cynthia B.", "prenom": "", "branches": ["Live"], "sous_branche": None, "niveau_technicien": "Intermédiaire", "niveau_acces": "Technicien"},
            {"nom": "Victor", "prenom": "", "branches": ["Live"], "sous_branche": None, "niveau_technicien": "Confirmé", "niveau_acces": "Gestionnaire"},
            {"nom": "Magda", "prenom": "", "branches": ["Live"], "sous_branche": None, "niveau_technicien": "Intermédiaire", "niveau_acces": "Technicien"},
            {"nom": "James", "prenom": "", "branches": ["Live"], "sous_branche": None, "niveau_technicien": "Confirmé", "niveau_acces": "Gestionnaire"},
            {"nom": "Prisca", "prenom": "", "branches": ["Live"], "sous_branche": None, "niveau_technicien": "Intermédiaire", "niveau_acces": "Technicien"},
            {"nom": "Daniel N", "prenom": "", "branches": ["Live"], "sous_branche": None, "niveau_technicien": "Intermédiaire", "niveau_acces": "Technicien"},
            {"nom": "Guichard", "prenom": "", "branches": ["Live", "Supervision"], "sous_branche": None, "niveau_technicien": "Expert", "niveau_acces": "Super Admin"},
            {"nom": "Darwin", "prenom": "", "branches": ["Live"], "sous_branche": None, "niveau_technicien": "Confirmé", "niveau_acces": "Gestionnaire"},
            {"nom": "Elder", "prenom": "", "branches": ["Live"], "sous_branche": None, "niveau_technicien": "Confirmé", "niveau_acces": "Gestionnaire"},
            {"nom": "Yedidjah", "prenom": "", "branches": ["Live"], "sous_branche": None, "niveau_technicien": "Intermédiaire", "niveau_acces": "Technicien"},
            {"nom": "Jovani", "prenom": "", "branches": ["Live"], "sous_branche": None, "niveau_technicien": "Intermédiaire", "niveau_acces": "Technicien"},
            # Incrustation
            {"nom": "Sybiline", "prenom": "", "branches": ["Live"], "sous_branche": "Incrustation", "niveau_technicien": "Confirmé", "niveau_acces": "Gestionnaire"},
            {"nom": "Joelle", "prenom": "", "branches": ["Live"], "sous_branche": "Incrustation", "niveau_technicien": "Intermédiaire", "niveau_acces": "Technicien"},
            {"nom": "Esther", "prenom": "", "branches": ["Live"], "sous_branche": "Incrustation", "niveau_technicien": "Intermédiaire", "niveau_acces": "Technicien"},
            {"nom": "Angelo", "prenom": "", "branches": ["Live"], "sous_branche": "Incrustation", "niveau_technicien": "Expert", "niveau_acces": "Responsable"},
            {"nom": "Yuna", "prenom": "", "branches": ["Live"], "sous_branche": "Incrustation", "niveau_technicien": "Débutant", "niveau_acces": "Technicien"},
            # VFX
            {"nom": "Fabrice", "prenom": "", "branches": ["Animation"], "sous_branche": None, "niveau_technicien": "Confirmé", "niveau_acces": "Gestionnaire"},
            {"nom": "Laura", "prenom": "", "branches": ["Animation"], "sous_branche": None, "niveau_technicien": "Expert", "niveau_acces": "Responsable"},
            {"nom": "Josué", "prenom": "", "branches": ["Animation"], "sous_branche": None, "niveau_technicien": "Intermédiaire", "niveau_acces": "Technicien"},
            {"nom": "Sara", "prenom": "", "branches": ["Animation"], "sous_branche": None, "niveau_technicien": "Intermédiaire", "niveau_acces": "Technicien"},
            {"nom": "Nicky", "prenom": "", "branches": ["Animation"], "sous_branche": None, "niveau_technicien": "Confirmé", "niveau_acces": "Gestionnaire"},
            {"nom": "Océane", "prenom": "", "branches": ["Animation"], "sous_branche": None, "niveau_technicien": "Intermédiaire", "niveau_acces": "Technicien"},
            # Cadreurs
            {"nom": "Bérénice", "prenom": "", "branches": ["Live"], "sous_branche": "Cadreur", "niveau_technicien": "Confirmé", "niveau_acces": "Gestionnaire"},
            {"nom": "Rebecca", "prenom": "", "branches": ["Live"], "sous_branche": "Cadreur", "niveau_technicien": "Confirmé", "niveau_acces": "Gestionnaire"},
            {"nom": "Martine", "prenom": "", "branches": ["Live"], "sous_branche": "Cadreur", "niveau_technicien": "Intermédiaire", "niveau_acces": "Technicien"},
            {"nom": "Ethan", "prenom": "", "branches": ["Live"], "sous_branche": "Cadreur", "niveau_technicien": "Intermédiaire", "niveau_acces": "Technicien"},
            {"nom": "Pamela", "prenom": "", "branches": ["Live"], "sous_branche": "Cadreur", "niveau_technicien": "Intermédiaire", "niveau_acces": "Technicien"},
            {"nom": "Grace", "prenom": "", "branches": ["Live"], "sous_branche": "Cadreur", "niveau_technicien": "Intermédiaire", "niveau_acces": "Technicien"},
            {"nom": "Stacy", "prenom": "", "branches": ["Live"], "sous_branche": "Cadreur", "niveau_technicien": "Intermédiaire", "niveau_acces": "Technicien"},
            {"nom": "Marie-Sonie", "prenom": "", "branches": ["Live"], "sous_branche": "Cadreur", "niveau_technicien": "Intermédiaire", "niveau_acces": "Technicien"},
            {"nom": "Jean-Wisler", "prenom": "", "branches": ["Live"], "sous_branche": "Cadreur", "niveau_technicien": "Confirmé", "niveau_acces": "Gestionnaire"},
            {"nom": "Jacob", "prenom": "", "branches": ["Live"], "sous_branche": "Cadreur", "niveau_technicien": "Expert", "niveau_acces": "Responsable"},
            {"nom": "Brice", "prenom": "", "branches": ["Live"], "sous_branche": "Cadreur", "niveau_technicien": "Intermédiaire", "niveau_acces": "Technicien"},
            {"nom": "Cédric N.", "prenom": "", "branches": ["Live"], "sous_branche": "Cadreur", "niveau_technicien": "Intermédiaire", "niveau_acces": "Technicien"},
            {"nom": "Cynthia M.", "prenom": "", "branches": ["Live"], "sous_branche": "Cadreur", "niveau_technicien": "Intermédiaire", "niveau_acces": "Technicien"},
            {"nom": "Motler", "prenom": "", "branches": ["Live"], "sous_branche": "Cadreur", "niveau_technicien": "Confirmé", "niveau_acces": "Gestionnaire"},
            {"nom": "Isabelle", "prenom": "", "branches": ["Live"], "sous_branche": "Cadreur", "niveau_technicien": "Intermédiaire", "niveau_acces": "Technicien"},
            {"nom": "Junior", "prenom": "", "branches": ["Live"], "sous_branche": "Cadreur", "niveau_technicien": "Intermédiaire", "niveau_acces": "Technicien"},
            {"nom": "Brunel", "prenom": "", "branches": ["Live"], "sous_branche": "Cadreur", "niveau_technicien": "Intermédiaire", "niveau_acces": "Technicien"},
            {"nom": "Camille", "prenom": "", "branches": ["Live"], "sous_branche": "Cadreur", "niveau_technicien": "Intermédiaire", "niveau_acces": "Technicien"},
            {"nom": "Marc-Arthur", "prenom": "", "branches": ["Live"], "sous_branche": "Cadreur", "niveau_technicien": "Confirmé", "niveau_acces": "Gestionnaire"},
            {"nom": "Asony", "prenom": "", "branches": ["Live"], "sous_branche": "Cadreur", "niveau_technicien": "Intermédiaire", "niveau_acces": "Technicien"},
            {"nom": "Daniel JP", "prenom": "", "branches": ["Live"], "sous_branche": "Cadreur", "niveau_technicien": "Intermédiaire", "niveau_acces": "Technicien"},
            {"nom": "Frandjy", "prenom": "", "branches": ["Live"], "sous_branche": "Cadreur", "niveau_technicien": "Intermédiaire", "niveau_acces": "Technicien"},
            # Régisseurs
            {"nom": "Christel", "prenom": "", "branches": ["Production"], "sous_branche": None, "niveau_technicien": "Expert", "niveau_acces": "Responsable"},
            {"nom": "Danarocks", "prenom": "", "branches": ["Production"], "sous_branche": None, "niveau_technicien": "Confirmé", "niveau_acces": "Gestionnaire"},
            {"nom": "Nicolas", "prenom": "", "branches": ["Production", "Logistique"], "sous_branche": None, "niveau_technicien": "Confirmé", "niveau_acces": "Gestionnaire"},
            {"nom": "Elvis", "prenom": "", "branches": ["Production"], "sous_branche": None, "niveau_technicien": "Intermédiaire", "niveau_acces": "Technicien"},
            {"nom": "Emmanuella", "prenom": "", "branches": ["Production"], "sous_branche": None, "niveau_technicien": "Intermédiaire", "niveau_acces": "Technicien"},
            {"nom": "Eloise", "prenom": "", "branches": ["Production"], "sous_branche": None, "niveau_technicien": "Intermédiaire", "niveau_acces": "Technicien"},
            {"nom": "Edese", "prenom": "", "branches": ["Production"], "sous_branche": None, "niveau_technicien": "Intermédiaire", "niveau_acces": "Technicien"},
            {"nom": "Sherley", "prenom": "", "branches": ["Production"], "sous_branche": None, "niveau_technicien": "Intermédiaire", "niveau_acces": "Technicien"},
            {"nom": "Judite", "prenom": "", "branches": ["Production"], "sous_branche": None, "niveau_technicien": "Intermédiaire", "niveau_acces": "Technicien"},
            {"nom": "Joanna", "prenom": "", "branches": ["Logistique"], "sous_branche": None, "niveau_technicien": "Expert", "niveau_acces": "Responsable"},
            # Diffusion - Renaud is Responsable de la Diffusion
            {"nom": "Renaud", "prenom": "", "branches": ["Live"], "sous_branche": "Diffusion", "niveau_technicien": "Expert", "niveau_acces": "Responsable"},
            {"nom": "Harvey", "prenom": "", "branches": ["Live"], "sous_branche": "Diffusion", "niveau_technicien": "Confirmé", "niveau_acces": "Gestionnaire"},
            {"nom": "Jean-Remy Victor", "prenom": "", "branches": ["Live"], "sous_branche": "Diffusion", "niveau_technicien": "Confirmé", "niveau_acces": "Gestionnaire"},
            {"nom": "Cedric", "prenom": "", "branches": ["Live"], "sous_branche": "Diffusion", "niveau_technicien": "Intermédiaire", "niveau_acces": "Technicien"},
            {"nom": "Dierry", "prenom": "", "branches": ["Live"], "sous_branche": "Diffusion", "niveau_technicien": "Intermédiaire", "niveau_acces": "Technicien"},
            {"nom": "Tresor", "prenom": "", "branches": ["Live"], "sous_branche": "Diffusion", "niveau_technicien": "Intermédiaire", "niveau_acces": "Technicien"},
            {"nom": "Michael", "prenom": "", "branches": ["Live"], "sous_branche": "Diffusion", "niveau_technicien": "Intermédiaire", "niveau_acces": "Technicien"},
            {"nom": "Yves", "prenom": "", "branches": ["Live"], "sous_branche": "Diffusion", "niveau_technicien": "Intermédiaire", "niveau_acces": "Technicien"},
            {"nom": "Esdras", "prenom": "", "branches": ["Live"], "sous_branche": "Diffusion", "niveau_technicien": "Intermédiaire", "niveau_acces": "Technicien"},
            {"nom": "Joseph", "prenom": "", "branches": ["Live"], "sous_branche": "Diffusion", "niveau_technicien": "Intermédiaire", "niveau_acces": "Technicien"},
            # Coordination
            {"nom": "Delphine", "prenom": "", "branches": ["Coordination"], "sous_branche": None, "niveau_technicien": "Expert", "niveau_acces": "Responsable"},
            {"nom": "Winchel", "prenom": "", "branches": ["Coordination"], "sous_branche": None, "niveau_technicien": "Expert", "niveau_acces": "Responsable"},
            {"nom": "Paul Baptista", "prenom": "", "branches": ["Supervision"], "sous_branche": None, "niveau_technicien": "Expert", "niveau_acces": "Admin"},
            {"nom": "Ryan", "prenom": "", "branches": ["Supervision"], "sous_branche": None, "niveau_technicien": "Expert", "niveau_acces": "Admin"},
            # Additional for FCP/Intercom
            {"nom": "Paul", "prenom": "", "branches": ["Live"], "sous_branche": None, "niveau_technicien": "Confirmé", "niveau_acces": "Gestionnaire"},
            {"nom": "Tchaba", "prenom": "", "branches": ["Live"], "sous_branche": None, "niveau_technicien": "Intermédiaire", "niveau_acces": "Technicien"},
            {"nom": "Coralie", "prenom": "", "branches": ["Live"], "sous_branche": None, "niveau_technicien": "Intermédiaire", "niveau_acces": "Technicien"},
            {"nom": "Balikissou", "prenom": "", "branches": ["Live"], "sous_branche": None, "niveau_technicien": "Intermédiaire", "niveau_acces": "Technicien"},
        ]
        
        for t in techniciens_data:
            tech = {
                "id": str(uuid.uuid4()),
                "nom": t["nom"],
                "prenom": t.get("prenom", ""),
                "niveau_technicien": t["niveau_technicien"],
                "niveau_acces": t["niveau_acces"],
                "branches": t["branches"],
                "sous_branches": ([t["sous_branche"]] if t.get("sous_branche") else []),
                "badge_attribue": False,
                "telephone": None,
                "email": None,
                "is_archived": False,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "updated_at": datetime.now(timezone.utc).isoformat()
            }
            await db.techniciens.insert_one(tech)
        logger.info(f"Seeded {len(techniciens_data)} techniciens")

    # Seed salles
    salles_count = await db.salles.count_documents({})
    if salles_count == 0:
        salles_data = [
            {"nom": "Sanctuaire", "capacite": 2000, "equipements": "Sono, Vidéoprojecteur, Écrans LED", "description": "Salle principale de culte"},
            {"nom": "Salle 114", "capacite": 50, "equipements": "Tableau, Vidéoprojecteur", "description": "Salle de réunion PAV"},
            {"nom": "Salle Annexe", "capacite": 300, "equipements": "Sono, Écrans", "description": "Salle annexe pour overflow"},
            {"nom": "Poly 3", "capacite": 150, "equipements": "Sono, Écran", "description": "Salle polyvalente"},
            {"nom": "Gymnase", "capacite": 500, "equipements": "Sono", "description": "Gymnase pour grands événements"},
        ]
        for s in salles_data:
            salle = {
                "id": str(uuid.uuid4()),
                **s,
                "is_archived": False,
                "created_at": datetime.now(timezone.utc).isoformat()
            }
            await db.salles.insert_one(salle)
        logger.info(f"Seeded {len(salles_data)} salles")

    # Seed creneaux
    creneaux_count = await db.creneaux.count_documents({})
    if creneaux_count == 0:
        creneaux_data = [
            {"nom": "Matin", "heure_debut": "08:00", "heure_fin": "12:00"},
            {"nom": "Après-midi", "heure_debut": "14:00", "heure_fin": "18:00"},
            {"nom": "Soir", "heure_debut": "18:00", "heure_fin": "22:00"},
            {"nom": "Journée complète", "heure_debut": "08:00", "heure_fin": "22:00"},
        ]
        for c in creneaux_data:
            creneau = {
                "id": str(uuid.uuid4()),
                **c,
                "is_active": True,
                "created_at": datetime.now(timezone.utc).isoformat()
            }
            await db.creneaux.insert_one(creneau)
        logger.info(f"Seeded {len(creneaux_data)} creneaux")

    # Seed document categories
    doc_cat_count = await db.document_categories.count_documents({})
    if doc_cat_count == 0:
        categories = [
            {"nom": "Plans", "description": "Plans de scène, plans techniques"},
            {"nom": "Procédures", "description": "Guides et procédures techniques"},
            {"nom": "Formations", "description": "Documents de formation"},
            {"nom": "Archives", "description": "Documents archivés"},
        ]
        for cat in categories:
            doc_cat = {
                "id": str(uuid.uuid4()),
                **cat,
                "created_at": datetime.now(timezone.utc).isoformat()
            }
            await db.document_categories.insert_one(doc_cat)
        logger.info("Seeded document categories")

# ==================== AUTH ROUTES ====================

@api_router.post("/auth/login", response_model=TokenResponse)
async def login(data: UserLogin):
    user = await db.users.find_one({"username": data.username}, {"_id": 0})
    if not user or not verify_password(data.password, user['password']):
        raise HTTPException(status_code=401, detail="Identifiants invalides")
    if not user.get('is_active', True):
        raise HTTPException(status_code=401, detail="Compte désactivé")
    
    token = create_token(user['id'], user['username'], user['niveau_acces'])
    await log_action(user['id'], user['full_name'], "Connexion", f"Connexion de {user['username']}")
    
    return TokenResponse(
        access_token=token,
        token_type="bearer",
        user=UserResponse(
            id=user['id'],
            username=user['username'],
            full_name=user['full_name'],
            niveau_acces=user['niveau_acces'],
            branches=user.get('branches', []),
            technicien_id=user.get('technicien_id'),
            created_at=user['created_at'],
            is_active=user.get('is_active', True),
            onboarding_seen=user.get('onboarding_seen', True)
        )
    )

@api_router.get("/auth/me", response_model=UserResponse)
async def get_me(current_user: dict = Depends(get_current_user)):
    return UserResponse(
        id=current_user['id'],
        username=current_user['username'],
        full_name=current_user['full_name'],
        niveau_acces=current_user['niveau_acces'],
        branches=current_user.get('branches', []),
        technicien_id=current_user.get('technicien_id'),
        created_at=current_user['created_at'],
        is_active=current_user.get('is_active', True),
        must_change_password=current_user.get('must_change_password', False),
        onboarding_seen=current_user.get('onboarding_seen', True),
        badge_status=current_user.get('badge_status'),
        badge_photo_url=current_user.get('badge_photo_url'),
        badge_is_renewal=current_user.get('badge_is_renewal', False),
        badge_requested_at=current_user.get('badge_requested_at'),
        badge_reviewed_at=current_user.get('badge_reviewed_at'),
        badge_reviewed_by_name=current_user.get('badge_reviewed_by_name'),
        badge_message=current_user.get('badge_message'),
        badge_motif=current_user.get('badge_motif')
    )

@api_router.put("/auth/me/onboarding-seen")
async def mark_onboarding_seen(current_user: dict = Depends(get_current_user)):
    """Called once the first-login onboarding guide popup has been
    dismissed, so it never shows again for this account."""
    await db.users.update_one({"id": current_user['id']}, {"$set": {"onboarding_seen": True}})
    return {"message": "Onboarding marqué comme vu"}

# ==================== RGPD — SELF-SERVICE DATA RIGHTS ====================

@api_router.get("/me/export")
async def export_my_data(current_user: dict = Depends(get_current_user)):
    """RGPD droit d'accès / portabilité : renvoie l'intégralité des données
    personnelles liées au compte de l'utilisateur connecté, dans un format
    exploitable (JSON)."""
    technicien = None
    if current_user.get('technicien_id'):
        technicien = await db.techniciens.find_one({"id": current_user['technicien_id']}, {"_id": 0})
    absences = await db.absences.find({"user_id": current_user['id']}, {"_id": 0}).to_list(1000)
    logs = await db.logs.find({"user_id": current_user['id']}, {"_id": 0}).sort("timestamp", -1).to_list(500)
    return {
        "compte": {
            "id": current_user['id'],
            "username": current_user['username'],
            "full_name": current_user['full_name'],
            "niveau_acces": current_user['niveau_acces'],
            "branches": current_user.get('branches', []),
            "created_at": current_user['created_at'],
        },
        "profil_technicien": technicien,
        "absences_declarees": absences,
        "journal_activite": logs,
        "genere_le": datetime.now(timezone.utc).isoformat(),
    }

@api_router.post("/me/delete-request")
async def request_account_deletion(current_user: dict = Depends(get_current_user)):
    """RGPD droit à l'effacement : l'utilisateur ne peut pas supprimer lui-même
    son compte (les comptes sont liés à l'historique Planning/Devis/Formations,
    dont la conservation répond à un intérêt légitime de gestion du
    département), mais peut déclencher une demande tracée qu'un Super Admin
    traitera (anonymisation ou suppression) sous 30 jours."""
    await db.users.update_one(
        {"id": current_user['id']},
        {"$set": {"deletion_requested": True, "deletion_requested_at": datetime.now(timezone.utc).isoformat()}}
    )
    await log_action(current_user['id'], current_user['full_name'], "Demande de suppression RGPD", "Demande d'effacement du compte")
    fire_and_forget_email(ADMIN_NOTIFY_EMAIL, "Demande RGPD — suppression de compte", email_template(
        "Un utilisateur demande la suppression de son compte",
        [f"<b>Utilisateur :</b> {current_user['full_name']} ({current_user['username']})",
         "À traiter sous 30 jours conformément au RGPD, depuis Administration → Utilisateurs."],
        kind="pending"
    ))
    super_admin_ids = await get_user_ids_by_roles(["Super Admin"])
    await create_notification(
        super_admin_ids, "suppression_compte",
        "Demande de suppression de compte",
        f"{current_user['full_name']} ({current_user['username']}) demande la suppression de son compte (RGPD, à traiter sous 30 jours).",
        link="/administration"
    )
    return {"message": "Votre demande de suppression a été transmise à l'administration et sera traitée sous 30 jours."}

@api_router.post("/auth/users", response_model=UserResponse)
async def create_user(data: UserCreate, current_user: dict = Depends(get_current_user)):
    check_access(current_user, ["Super Admin"])
    existing = await db.users.find_one({"username": data.username})
    if existing:
        raise HTTPException(status_code=400, detail="Nom d'utilisateur déjà pris")
    
    user = {
        "id": str(uuid.uuid4()),
        "username": data.username,
        "password": hash_password(data.password),
        "full_name": data.full_name,
        "niveau_acces": data.niveau_acces,
        "branches": data.branches or [],
        "created_at": datetime.now(timezone.utc).isoformat(),
        "is_active": True,
        "must_change_password": True,  # Force password change on first login
        "onboarding_seen": False  # Show the first-login onboarding guide once
    }
    await db.users.insert_one(user)
    await log_action(current_user['id'], current_user['full_name'], "Création utilisateur", f"Utilisateur créé: {data.username}")

    return UserResponse(
        id=user['id'], username=user['username'], full_name=user['full_name'],
        niveau_acces=user['niveau_acces'], branches=user['branches'], created_at=user['created_at'], is_active=user['is_active'],
        must_change_password=user['must_change_password'], onboarding_seen=user['onboarding_seen']
    )

@api_router.put("/auth/users/{user_id}")
async def update_user(user_id: str, data: UserUpdate, current_user: dict = Depends(get_current_user)):
    check_access(current_user, ["Super Admin"])
    target = await db.users.find_one({"id": user_id}, {"_id": 0})
    if not target:
        raise HTTPException(status_code=404, detail="Utilisateur non trouvé")
    if is_protected_account(target) and data.niveau_acces and data.niveau_acces != "Super Admin":
        raise HTTPException(status_code=403, detail="Ce compte est protégé : son niveau d'accès ne peut pas être changé depuis cette action.")

    update_data = {}
    if data.full_name:
        update_data["full_name"] = data.full_name
    if data.niveau_acces:
        update_data["niveau_acces"] = data.niveau_acces
    if data.branches is not None:
        update_data["branches"] = data.branches

    if not update_data:
        raise HTTPException(status_code=400, detail="Aucune donnée à modifier")

    result = await db.users.update_one({"id": user_id}, {"$set": update_data})
    if result.modified_count == 0:
        raise HTTPException(status_code=404, detail="Utilisateur non trouvé")
    await log_action(current_user['id'], current_user['full_name'], "Modification utilisateur", f"Utilisateur modifié: {user_id}")
    user = await db.users.find_one({"id": user_id}, {"_id": 0, "password": 0})
    return UserResponse(**user)

@api_router.post("/auth/change-password")
async def change_password(data: PasswordChange, current_user: dict = Depends(get_current_user)):
    if len(data.new_password) < 6:
        raise HTTPException(status_code=400, detail="Le mot de passe doit faire au moins 6 caractères")
    
    await db.users.update_one(
        {"id": current_user['id']},
        {"$set": {"password": hash_password(data.new_password), "must_change_password": False}}
    )
    await log_action(current_user['id'], current_user['full_name'], "Changement mot de passe", "Mot de passe modifié")
    return {"message": "Mot de passe modifié"}

@api_router.get("/auth/users", response_model=List[UserResponse])
async def get_users(current_user: dict = Depends(get_current_user)):
    check_access(current_user, ["Super Admin", "Admin"])
    # Le compte de secours (hidden_account) n'apparaît que pour son titulaire
    # (Guichard) — voir PROTECTED_USERNAMES plus haut.
    query = {} if is_owner_account(current_user) else {"hidden_account": {"$ne": True}}
    users = await db.users.find(query, {"_id": 0, "password": 0}).to_list(1000)
    return [UserResponse(**u) for u in users]

@api_router.delete("/auth/users/{user_id}")
async def delete_user(user_id: str, current_user: dict = Depends(get_current_user)):
    check_access(current_user, ["Super Admin"])
    if current_user['id'] == user_id:
        raise HTTPException(status_code=400, detail="Impossible de supprimer votre propre compte")
    target = await db.users.find_one({"id": user_id}, {"_id": 0})
    if not target:
        raise HTTPException(status_code=404, detail="Utilisateur non trouvé")
    assert_account_not_protected(target)
    result = await db.users.delete_one({"id": user_id})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Utilisateur non trouvé")
    await log_action(current_user['id'], current_user['full_name'], "Suppression utilisateur", f"Utilisateur supprimé: {user_id}")
    return {"message": "Utilisateur supprimé"}

@api_router.put("/auth/users/{user_id}/reset-password")
async def reset_password(user_id: str, data: ResetPasswordRequest, current_user: dict = Depends(get_current_user)):
    check_access(current_user, ["Super Admin"])
    # First check if user exists
    user = await db.users.find_one({"id": user_id})
    if not user:
        raise HTTPException(status_code=404, detail="Utilisateur non trouvé")
    if is_protected_account(user) and current_user['id'] != user['id']:
        raise HTTPException(status_code=403, detail="Ce compte est protégé : son mot de passe ne peut être changé que par son titulaire, via Mon espace.")

    await db.users.update_one(
        {"id": user_id},
        {"$set": {"password": hash_password(data.new_password), "must_change_password": True}}
    )
    await log_action(current_user['id'], current_user['full_name'], "Reset mot de passe", f"Mot de passe réinitialisé pour: {user_id}")
    return {"message": "Mot de passe réinitialisé"}

@api_router.put("/auth/users/{user_id}/toggle-active")
async def toggle_user_active(user_id: str, current_user: dict = Depends(get_current_user)):
    check_access(current_user, ["Super Admin"])
    user = await db.users.find_one({"id": user_id}, {"_id": 0})
    if not user:
        raise HTTPException(status_code=404, detail="Utilisateur non trouvé")
    new_status = not user.get('is_active', True)
    if is_protected_account(user) and not new_status:
        raise HTTPException(status_code=403, detail="Ce compte est protégé et ne peut pas être désactivé.")
    await db.users.update_one({"id": user_id}, {"$set": {"is_active": new_status}})
    await log_action(current_user['id'], current_user['full_name'], "Changement statut utilisateur", f"Statut changé: {user_id} -> {'Actif' if new_status else 'Inactif'}")
    return {"message": f"Utilisateur {'activé' if new_status else 'désactivé'}"}

@api_router.put("/auth/users/{user_id}/activate")
async def activate_user(user_id: str, current_user: dict = Depends(get_current_user)):
    check_access(current_user, ["Super Admin"])
    result = await db.users.update_one({"id": user_id}, {"$set": {"is_active": True}})
    if result.modified_count == 0:
        raise HTTPException(status_code=404, detail="Utilisateur non trouvé")
    await log_action(current_user['id'], current_user['full_name'], "Activation utilisateur", f"Utilisateur activé: {user_id}")
    return {"message": "Utilisateur activé"}

@api_router.put("/auth/users/{user_id}/deactivate")
async def deactivate_user(user_id: str, current_user: dict = Depends(get_current_user)):
    check_access(current_user, ["Super Admin"])
    target = await db.users.find_one({"id": user_id}, {"_id": 0})
    if target:
        assert_account_not_protected(target)
    result = await db.users.update_one({"id": user_id}, {"$set": {"is_active": False}})
    if result.modified_count == 0:
        raise HTTPException(status_code=404, detail="Utilisateur non trouvé")
    await log_action(current_user['id'], current_user['full_name'], "Désactivation utilisateur", f"Utilisateur désactivé: {user_id}")
    return {"message": "Utilisateur désactivé"}

@api_router.put("/auth/users/{user_id}/update-access")
async def update_user_access(user_id: str, niveau_acces: str, current_user: dict = Depends(get_current_user)):
    check_access(current_user, ["Super Admin"])
    if niveau_acces not in NIVEAUX_ACCES:
        raise HTTPException(status_code=400, detail="Niveau d'accès invalide")
    target = await db.users.find_one({"id": user_id}, {"_id": 0})
    if target and is_protected_account(target) and niveau_acces != "Super Admin":
        raise HTTPException(status_code=403, detail="Ce compte est protégé : son niveau d'accès ne peut pas être changé.")
    result = await db.users.update_one({"id": user_id}, {"$set": {"niveau_acces": niveau_acces}})
    if result.modified_count == 0:
        raise HTTPException(status_code=404, detail="Utilisateur non trouvé")
    await log_action(current_user['id'], current_user['full_name'], "Changement niveau accès", f"Niveau changé pour {user_id}: {niveau_acces}")
    return {"message": "Niveau d'accès mis à jour"}

# ==================== SELF-REGISTRATION ====================
# Lets a technicien already listed in the Effectif create their own login
# without a Super Admin having to do it. They pick their name from the list
# of techniciens that don't have an account yet, then set their own password.
# This is intentionally public (no auth) since it runs before login.

@api_router.get("/techniciens/unclaimed")
async def get_unclaimed_techniciens():
    techniciens = await db.techniciens.find({"is_archived": False}, {"_id": 0, "id": 1, "nom": 1, "prenom": 1}).sort("nom", 1).to_list(1000)
    claimed_ids = set()
    async for u in db.users.find({"technicien_id": {"$ne": None}}, {"_id": 0, "technicien_id": 1}):
        claimed_ids.add(u.get("technicien_id"))
    return [t for t in techniciens if t["id"] not in claimed_ids]

@api_router.post("/auth/register", response_model=TokenResponse)
async def register(data: RegisterRequest):
    technicien = await db.techniciens.find_one({"id": data.technicien_id, "is_archived": False}, {"_id": 0})
    if not technicien:
        raise HTTPException(status_code=404, detail="Technicien introuvable")

    already_claimed = await db.users.find_one({"technicien_id": data.technicien_id})
    if already_claimed:
        raise HTTPException(status_code=400, detail="Un compte existe déjà pour ce technicien")

    existing_username = await db.users.find_one({"username": data.username})
    if existing_username:
        raise HTTPException(status_code=400, detail="Nom d'utilisateur déjà pris")

    if len(data.password) < 6:
        raise HTTPException(status_code=400, detail="Le mot de passe doit faire au moins 6 caractères")

    full_name = f"{technicien.get('prenom', '')} {technicien.get('nom', '')}".strip()
    user = {
        "id": str(uuid.uuid4()),
        "username": data.username,
        "password": hash_password(data.password),
        "full_name": full_name,
        "niveau_acces": "Technicien",
        "branches": technicien.get("branches", []),
        "technicien_id": technicien["id"],
        "created_at": datetime.now(timezone.utc).isoformat(),
        "is_active": True,
        "must_change_password": False,
        "onboarding_seen": False  # Show the first-login onboarding guide once
    }
    await db.users.insert_one(user)
    await log_action(user['id'], user['full_name'], "Auto-inscription", f"Compte créé par {full_name}")

    token = create_token(user['id'], user['username'], user['niveau_acces'])
    return TokenResponse(
        access_token=token,
        token_type="bearer",
        user=UserResponse(
            id=user['id'], username=user['username'], full_name=user['full_name'],
            niveau_acces=user['niveau_acces'], branches=user['branches'], technicien_id=user['technicien_id'],
            created_at=user['created_at'], is_active=user['is_active'], must_change_password=user['must_change_password'],
            onboarding_seen=user['onboarding_seen']
        )
    )

# ==================== GROUPS ROUTES ====================

@api_router.get("/groups", response_model=List[GroupResponse])
async def get_groups(current_user: dict = Depends(get_current_user)):
    check_access(current_user, ["Super Admin", "Admin"])
    groups = await db.groups.find({}, {"_id": 0}).to_list(100)
    return [GroupResponse(**g) for g in groups]

@api_router.post("/groups", response_model=GroupResponse)
async def create_group(data: GroupCreate, current_user: dict = Depends(get_current_user)):
    check_access(current_user, ["Super Admin"])
    group = {
        "id": str(uuid.uuid4()),
        "name": data.name,
        "permissions": data.permissions,
        "created_at": datetime.now(timezone.utc).isoformat()
    }
    await db.groups.insert_one(group)
    await log_action(current_user['id'], current_user['full_name'], "Création groupe", f"Groupe créé: {data.name}")
    return GroupResponse(**group)

@api_router.delete("/groups/{group_id}")
async def delete_group(group_id: str, current_user: dict = Depends(get_current_user)):
    check_access(current_user, ["Super Admin"])
    result = await db.groups.delete_one({"id": group_id})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Groupe non trouvé")
    await log_action(current_user['id'], current_user['full_name'], "Suppression groupe", f"Groupe supprimé: {group_id}")
    return {"message": "Groupe supprimé"}

# ==================== TECHNICIENS ROUTES ====================

def normalize_technicien(t: dict) -> dict:
    """Convert old branche format to new branches format"""
    if 'branches' not in t and 'branche' in t:
        t['branches'] = [t['branche']] if t['branche'] else []
    if 'branche' in t:
        del t['branche']
    return t

@api_router.get("/techniciens", response_model=List[TechnicienResponse])
async def get_techniciens(include_archived: bool = False, current_user: dict = Depends(get_current_user)):
    query = {} if include_archived else {"is_archived": False}
    # Les fiches proposées par un Responsable et pas encore validées par la
    # Coordination n'apparaissent pas dans l'effectif "officiel" — elles ne
    # sont visibles que via /techniciens/pending.
    query["is_pending_approval"] = {"$ne": True}
    techniciens = await db.techniciens.find(query, {"_id": 0}).sort("nom", 1).to_list(1000)
    return [TechnicienResponse(**normalize_technicien(t)) for t in techniciens]

@api_router.get("/techniciens/pending", response_model=List[TechnicienResponse])
async def get_pending_techniciens(current_user: dict = Depends(get_current_user)):
    # Coordination = Gestionnaire+ (le rôle Responsable qui soumet la fiche
    # ne doit pas pouvoir se valider lui-même — d'où un droit "effectif.approve"
    # distinct de "effectif.write", explicitement interdit pour Responsable
    # dans la matrice de Droits d'accès).
    await check_access_or_permission(current_user, ["Super Admin", "Gestionnaire"], "effectif.approve")
    techs = await db.techniciens.find({"is_pending_approval": True}, {"_id": 0}).sort("created_at", -1).to_list(500)
    return [TechnicienResponse(**normalize_technicien(t)) for t in techs]

@api_router.get("/techniciens/{tech_id}", response_model=TechnicienResponse)
async def get_technicien(tech_id: str, current_user: dict = Depends(get_current_user)):
    tech = await db.techniciens.find_one({"id": tech_id}, {"_id": 0})
    if not tech:
        raise HTTPException(status_code=404, detail="Technicien introuvable")
    return TechnicienResponse(**normalize_technicien(tech))

@api_router.post("/techniciens", response_model=TechnicienResponse)
async def create_technicien(data: TechnicienCreate, current_user: dict = Depends(get_current_user)):
    await check_access_or_permission(current_user, ["Super Admin", "Responsable"], "effectif.write")
    # Une fiche créée par un Responsable est soumise à la Coordination avant
    # d'intégrer définitivement l'effectif. Super Admin (et quiconque passe
    # via un droit de groupe explicite) reste en création directe.
    is_responsable_submission = current_user['niveau_acces'] == "Responsable"
    tech = {
        "id": str(uuid.uuid4()),
        **data.model_dump(),
        "is_archived": False,
        "is_pending_approval": is_responsable_submission,
        "proposed_by": current_user['id'] if is_responsable_submission else None,
        "proposed_by_name": current_user['full_name'] if is_responsable_submission else None,
        "rejection_reason": None,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat()
    }
    await db.techniciens.insert_one(tech)
    if is_responsable_submission:
        await log_action(current_user['id'], current_user['full_name'], "Proposition fiche technicien", f"Fiche proposée (en attente de validation): {data.nom} {data.prenom}")
        coord_ids = await get_coordination_user_ids()
        await create_notification(
            coord_ids, "technicien", "Nouvelle fiche à valider",
            f"{current_user['full_name']} propose l'ajout de {data.nom} à l'effectif.",
            link="/effectif"
        )
    else:
        await log_action(current_user['id'], current_user['full_name'], "Création technicien", f"Technicien créé: {data.nom} {data.prenom}")
    return TechnicienResponse(**tech)

@api_router.post("/techniciens/{tech_id}/approve", response_model=TechnicienResponse)
async def approve_technicien(tech_id: str, current_user: dict = Depends(get_current_user)):
    await check_access_or_permission(current_user, ["Super Admin", "Gestionnaire"], "effectif.approve")
    tech = await db.techniciens.find_one({"id": tech_id}, {"_id": 0})
    if not tech:
        raise HTTPException(status_code=404, detail="Technicien introuvable")
    now = datetime.now(timezone.utc).isoformat()
    await db.techniciens.update_one({"id": tech_id}, {"$set": {
        "is_pending_approval": False, "rejection_reason": None, "updated_at": now
    }})
    await log_action(current_user['id'], current_user['full_name'], "Validation fiche technicien", f"Fiche validée: {tech['nom']}")
    if tech.get('proposed_by'):
        await create_notification(
            [tech['proposed_by']], "technicien", "Fiche validée",
            f"La fiche de {tech['nom']} a été validée et ajoutée à l'effectif.",
            link="/effectif"
        )
    updated = await db.techniciens.find_one({"id": tech_id}, {"_id": 0})
    return TechnicienResponse(**normalize_technicien(updated))

@api_router.post("/techniciens/{tech_id}/reject")
async def reject_technicien(tech_id: str, data: TechnicienRejectRequest, current_user: dict = Depends(get_current_user)):
    await check_access_or_permission(current_user, ["Super Admin", "Gestionnaire"], "effectif.approve")
    tech = await db.techniciens.find_one({"id": tech_id}, {"_id": 0})
    if not tech:
        raise HTTPException(status_code=404, detail="Technicien introuvable")
    await db.techniciens.delete_one({"id": tech_id})
    await log_action(current_user['id'], current_user['full_name'], "Rejet fiche technicien", f"Fiche rejetée: {tech['nom']}" + (f" — {data.message}" if data.message else ""))
    if tech.get('proposed_by'):
        msg = f"La fiche de {tech['nom']} n'a pas été retenue."
        if data.message:
            msg += f" Motif : {data.message}"
        await create_notification([tech['proposed_by']], "technicien", "Fiche non retenue", msg, link="/effectif")
    return {"message": "Fiche rejetée"}

@api_router.put("/techniciens/{tech_id}", response_model=TechnicienResponse)
async def update_technicien(tech_id: str, data: TechnicienCreate, current_user: dict = Depends(get_current_user)):
    await check_access_or_permission(current_user, ["Super Admin", "Responsable"], "effectif.write")
    update_data = data.model_dump()
    update_data["updated_at"] = datetime.now(timezone.utc).isoformat()
    # Remove old branche field if it exists
    await db.techniciens.update_one({"id": tech_id}, {"$unset": {"branche": ""}})
    result = await db.techniciens.update_one({"id": tech_id}, {"$set": update_data})
    if result.modified_count == 0:
        raise HTTPException(status_code=404, detail="Technicien non trouvé")
    tech = await db.techniciens.find_one({"id": tech_id}, {"_id": 0})
    await log_action(current_user['id'], current_user['full_name'], "Modification technicien", f"Technicien modifié: {data.nom}")
    return TechnicienResponse(**normalize_technicien(tech))

@api_router.put("/techniciens/{tech_id}/archive")
async def archive_technicien(tech_id: str, current_user: dict = Depends(get_current_user)):
    await check_access_or_permission(current_user, ["Super Admin"], "effectif.delete")
    result = await db.techniciens.update_one({"id": tech_id}, {"$set": {"is_archived": True, "updated_at": datetime.now(timezone.utc).isoformat()}})
    if result.modified_count == 0:
        raise HTTPException(status_code=404, detail="Technicien non trouvé")
    await log_action(current_user['id'], current_user['full_name'], "Archivage technicien", f"Technicien archivé: {tech_id}")
    return {"message": "Technicien archivé"}

@api_router.delete("/techniciens/{tech_id}")
async def delete_technicien(tech_id: str, current_user: dict = Depends(get_current_user)):
    check_access(current_user, ["Super Admin"])
    result = await db.techniciens.delete_one({"id": tech_id})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Technicien non trouvé")
    await log_action(current_user['id'], current_user['full_name'], "Suppression technicien", f"Technicien supprimé: {tech_id}")
    return {"message": "Technicien supprimé"}

# ==================== BADGE (demande / renouvellement) ====================
# Self-service workflow: any logged-in user uploads a clear, face-forward
# photo (ID-photo style) to request a first badge or renew an existing one.
# Gestionnaire+ then reviews it from Effectif — confirms receipt/validates,
# or flags it as non-compliant with a message back to the member. Every new
# submission raises an in-app notification for Gestionnaire+.
#
# Stored on the USER document (not the technicien record): most accounts on
# this deployment were created directly by an admin via "Nouvel utilisateur"
# and were never linked to a technicien_id (that link is only ever set by the
# self-registration flow). Keying badges off the user id instead means the
# feature works for every account regardless of whether a technicien link
# exists.

BADGE_ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg'}

class BadgeUserResponse(BaseModel):
    id: str
    username: str
    full_name: str
    badge_status: Optional[str] = None
    badge_photo_url: Optional[str] = None
    badge_is_renewal: Optional[bool] = False
    badge_requested_at: Optional[str] = None
    badge_reviewed_at: Optional[str] = None
    badge_reviewed_by_name: Optional[str] = None
    badge_message: Optional[str] = None
    badge_motif: Optional[str] = None

@api_router.post("/me/badge")
async def submit_my_badge_request(
    photo: UploadFile = File(...),
    motif: Optional[str] = Form(None),
    current_user: dict = Depends(get_current_user)
):
    """Submit a badge request or renewal with a photo, for the currently
    logged-in account."""
    if not photo.filename or '.' not in photo.filename:
        raise HTTPException(status_code=400, detail="Aucune photo fournie")
    ext = photo.filename.rsplit('.', 1)[1].lower()
    if ext not in BADGE_ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=400, detail="Format non accepté — utilisez une photo JPG ou PNG")

    contents = await photo.read()
    if len(contents) > MAX_FILE_SIZE:
        raise HTTPException(status_code=400, detail="Photo trop volumineuse (max 10 MB)")

    unique_filename = f"{uuid.uuid4()}.{ext}"
    content_type = 'image/png' if ext == 'png' else 'image/jpeg'
    await db.files.insert_one({
        "id": unique_filename,
        "filename": photo.filename,
        "content_type": content_type,
        "data": contents,
        "size": len(contents),
        "uploaded_by": current_user['id'],
        "created_at": datetime.now(timezone.utc).isoformat()
    })
    photo_url = f"/api/uploads/{unique_filename}"

    is_renewal = current_user.get("badge_status") == "validee"
    now = datetime.now(timezone.utc).isoformat()
    await db.users.update_one({"id": current_user['id']}, {"$set": {
        "badge_status": "en_attente_validation",
        "badge_photo_url": photo_url,
        "badge_is_renewal": is_renewal,
        "badge_requested_at": now,
        "badge_reviewed_at": None,
        "badge_reviewed_by_name": None,
        "badge_message": None,
        "badge_motif": motif,
    }})

    # Keep the legacy technicien badge_attribue flag in sync when a link exists.
    if current_user.get('technicien_id'):
        await db.techniciens.update_one(
            {"id": current_user['technicien_id']},
            {"$set": {"updated_at": now}}
        )

    label = "Renouvellement de badge" if is_renewal else "Demande de badge"
    await log_action(current_user['id'], current_user['full_name'], label, current_user['full_name'])

    recipient_ids = await get_user_ids_by_roles(["Gestionnaire", "Responsable", "Admin", "Super Admin"])
    await create_notification(
        recipient_ids, "badge",
        label,
        f"{current_user['full_name']} a soumis une photo pour son badge.",
        link="/effectif"
    )
    return {"message": "Demande envoyée", "badge_status": "en_attente_validation", "badge_photo_url": photo_url}

@api_router.get("/admin/badges", response_model=List[BadgeUserResponse])
async def list_badge_requests(current_user: dict = Depends(get_current_user)):
    check_access(current_user, ["Gestionnaire", "Responsable", "Admin", "Super Admin"])
    users = await db.users.find(
        {"badge_status": {"$ne": None}}, {"_id": 0}
    ).sort("badge_requested_at", -1).to_list(1000)
    return [BadgeUserResponse(**u) for u in users]

@api_router.post("/admin/badges/{user_id}/confirm")
async def confirm_badge(user_id: str, current_user: dict = Depends(get_current_user)):
    """Marks the badge as validated/issued."""
    check_access(current_user, ["Gestionnaire", "Responsable", "Super Admin"])
    target = await db.users.find_one({"id": user_id}, {"_id": 0})
    if not target:
        raise HTTPException(status_code=404, detail="Utilisateur introuvable")
    now = datetime.now(timezone.utc).isoformat()
    await db.users.update_one({"id": user_id}, {"$set": {
        "badge_status": "validee",
        "badge_reviewed_at": now,
        "badge_reviewed_by_name": current_user['full_name'],
        "badge_message": None,
    }})

    # Reflect the validated badge in Effectif. Most accounts on this
    # deployment aren't linked to a technicien_id (that link is only set by
    # self-registration) — fall back to matching on name so the badge_attribue
    # flag shown in Effectif still updates for admin-created accounts.
    technicien_id = target.get('technicien_id')
    if not technicien_id:
        name = (target.get('full_name') or '').strip()
        if name:
            match = await db.techniciens.find_one(
                {"nom": {"$regex": f"^{re.escape(name)}$", "$options": "i"}, "is_archived": False},
                {"_id": 0, "id": 1}
            )
            if match:
                technicien_id = match["id"]
                await db.users.update_one({"id": user_id}, {"$set": {"technicien_id": technicien_id}})
    if technicien_id:
        await db.techniciens.update_one({"id": technicien_id}, {"$set": {"badge_attribue": True, "updated_at": now}})
    await log_action(current_user['id'], current_user['full_name'], "Validation badge", target.get('full_name'))

    await create_notification(
        [user_id], "badge",
        "Badge validé",
        "Votre badge a été validé.",
        link="/mon-espace"
    )
    return {"message": "Badge validé"}

@api_router.post("/admin/badges/{user_id}/reject")
async def reject_badge(user_id: str, data: BadgeRejectRequest, current_user: dict = Depends(get_current_user)):
    """Flags the submitted photo as non-compliant, with a message explaining
    why, sent back to the member so they can resubmit."""
    check_access(current_user, ["Gestionnaire", "Responsable", "Super Admin"])
    target = await db.users.find_one({"id": user_id}, {"_id": 0})
    if not target:
        raise HTTPException(status_code=404, detail="Utilisateur introuvable")
    now = datetime.now(timezone.utc).isoformat()
    await db.users.update_one({"id": user_id}, {"$set": {
        "badge_status": "non_conforme",
        "badge_reviewed_at": now,
        "badge_reviewed_by_name": current_user['full_name'],
        "badge_message": data.message,
    }})
    await log_action(current_user['id'], current_user['full_name'], "Photo badge refusée", f"{target.get('full_name')}: {data.message}")

    await create_notification(
        [user_id], "badge",
        "Photo de badge non conforme",
        data.message,
        link="/mon-espace"
    )
    return {"message": "Photo signalée comme non conforme"}

# ==================== MATERIEL ROUTES ====================

@api_router.get("/materiel", response_model=List[MaterielResponse])
async def get_materiel(include_archived: bool = False, current_user: dict = Depends(get_current_user)):
    query = {} if include_archived else {"is_archived": False}
    materiels = await db.materiel.find(query, {"_id": 0}).to_list(1000)
    return [MaterielResponse(**m) for m in materiels]

@api_router.post("/materiel", response_model=MaterielResponse)
async def create_materiel(data: MaterielCreate, current_user: dict = Depends(get_current_user)):
    await check_access_or_permission(current_user, ["Super Admin", "Responsable", "Gestionnaire"], "logistique.write")
    mat = {
        "id": str(uuid.uuid4()),
        **data.model_dump(),
        "is_archived": False,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat()
    }
    await db.materiel.insert_one(mat)
    await log_action(current_user['id'], current_user['full_name'], "Création matériel", f"Matériel créé: {data.nom}")
    return MaterielResponse(**mat)

@api_router.put("/materiel/{mat_id}", response_model=MaterielResponse)
async def update_materiel(mat_id: str, data: MaterielCreate, current_user: dict = Depends(get_current_user)):
    await check_access_or_permission(current_user, ["Super Admin", "Responsable", "Gestionnaire"], "logistique.write")
    update_data = data.model_dump()
    update_data["updated_at"] = datetime.now(timezone.utc).isoformat()
    result = await db.materiel.update_one({"id": mat_id}, {"$set": update_data})
    if result.modified_count == 0:
        raise HTTPException(status_code=404, detail="Matériel non trouvé")
    mat = await db.materiel.find_one({"id": mat_id}, {"_id": 0})
    await log_action(current_user['id'], current_user['full_name'], "Modification matériel", f"Matériel modifié: {data.nom}")
    return MaterielResponse(**mat)

@api_router.put("/materiel/{mat_id}/archive")
async def archive_materiel(mat_id: str, current_user: dict = Depends(get_current_user)):
    await check_access_or_permission(current_user, ["Super Admin"], "logistique.delete")
    result = await db.materiel.update_one({"id": mat_id}, {"$set": {"is_archived": True, "updated_at": datetime.now(timezone.utc).isoformat()}})
    if result.modified_count == 0:
        raise HTTPException(status_code=404, detail="Matériel non trouvé")
    await log_action(current_user['id'], current_user['full_name'], "Archivage matériel", f"Matériel archivé: {mat_id}")
    return {"message": "Matériel archivé"}

@api_router.delete("/materiel/{mat_id}")
async def delete_materiel(mat_id: str, current_user: dict = Depends(get_current_user)):
    check_access(current_user, ["Super Admin"])
    result = await db.materiel.delete_one({"id": mat_id})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Matériel non trouvé")
    await log_action(current_user['id'], current_user['full_name'], "Suppression matériel", f"Matériel supprimé: {mat_id}")
    return {"message": "Matériel supprimé"}

# ==================== MATERIEL CATEGORIES ROUTES ====================

@api_router.get("/materiel/categories")
async def get_materiel_categories(current_user: dict = Depends(get_current_user)):
    """Get all materiel categories (static + dynamic)"""
    # Get dynamic categories from DB
    dynamic_cats = await db.materiel_categories.find({}, {"_id": 0}).sort("nom", 1).to_list(100)
    dynamic_names = [c['nom'] for c in dynamic_cats]
    # Combine with static categories
    all_categories = list(set(CATEGORIES_MATERIEL + dynamic_names))
    all_categories.sort()
    return {"categories": all_categories}

@api_router.post("/materiel/categories")
async def create_materiel_category(data: dict, current_user: dict = Depends(get_current_user)):
    """Add a new dynamic materiel category"""
    check_access(current_user, ["Super Admin"])
    nom = data.get('nom', '').strip()
    if not nom:
        raise HTTPException(status_code=400, detail="Nom de catégorie requis")
    # Check if already exists
    existing = await db.materiel_categories.find_one({"nom": nom})
    if existing or nom in CATEGORIES_MATERIEL:
        raise HTTPException(status_code=400, detail="Cette catégorie existe déjà")
    category = {
        "id": str(uuid.uuid4()),
        "nom": nom,
        "created_at": datetime.now(timezone.utc).isoformat()
    }
    await db.materiel_categories.insert_one(category)
    await log_action(current_user['id'], current_user['full_name'], "Création catégorie matériel", f"Catégorie créée: {nom}")
    return {"message": "Catégorie créée", "id": category['id'], "nom": nom}

@api_router.delete("/materiel/categories/{category_name}")
async def delete_materiel_category(category_name: str, current_user: dict = Depends(get_current_user)):
    """Delete a dynamic materiel category"""
    check_access(current_user, ["Super Admin"])
    # Cannot delete static categories
    if category_name in CATEGORIES_MATERIEL:
        raise HTTPException(status_code=400, detail="Impossible de supprimer une catégorie prédéfinie")
    result = await db.materiel_categories.delete_one({"nom": category_name})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Catégorie non trouvée")
    await log_action(current_user['id'], current_user['full_name'], "Suppression catégorie matériel", f"Catégorie supprimée: {category_name}")
    return {"message": "Catégorie supprimée"}

# ==================== FOURNISSEURS ROUTES ====================

@api_router.get("/fournisseurs", response_model=List[FournisseurResponse])
async def get_fournisseurs(include_archived: bool = False, current_user: dict = Depends(get_current_user)):
    query = {} if include_archived else {"is_archived": False}
    fournisseurs = await db.fournisseurs.find(query, {"_id": 0}).to_list(1000)
    return [FournisseurResponse(**f) for f in fournisseurs]

@api_router.post("/fournisseurs", response_model=FournisseurResponse)
async def create_fournisseur(data: FournisseurCreate, current_user: dict = Depends(get_current_user)):
    check_access(current_user, ["Super Admin", "Responsable"])
    fournisseur = {
        "id": str(uuid.uuid4()),
        **data.model_dump(),
        "statut": "Actif",
        "is_archived": False,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat()
    }
    await db.fournisseurs.insert_one(fournisseur)
    await log_action(current_user['id'], current_user['full_name'], "Création fournisseur", f"Fournisseur créé: {data.nom}")
    return FournisseurResponse(**fournisseur)

@api_router.put("/fournisseurs/{fournisseur_id}", response_model=FournisseurResponse)
async def update_fournisseur(fournisseur_id: str, data: FournisseurCreate, current_user: dict = Depends(get_current_user)):
    check_access(current_user, ["Super Admin", "Responsable"])
    update_data = data.model_dump()
    update_data["updated_at"] = datetime.now(timezone.utc).isoformat()
    result = await db.fournisseurs.update_one({"id": fournisseur_id}, {"$set": update_data})
    if result.modified_count == 0:
        raise HTTPException(status_code=404, detail="Fournisseur non trouvé")
    fournisseur = await db.fournisseurs.find_one({"id": fournisseur_id}, {"_id": 0})
    await log_action(current_user['id'], current_user['full_name'], "Modification fournisseur", f"Fournisseur modifié: {data.nom}")
    return FournisseurResponse(**fournisseur)

@api_router.put("/fournisseurs/{fournisseur_id}/archive")
async def archive_fournisseur(fournisseur_id: str, current_user: dict = Depends(get_current_user)):
    check_access(current_user, ["Super Admin"])
    result = await db.fournisseurs.update_one({"id": fournisseur_id}, {"$set": {"is_archived": True, "updated_at": datetime.now(timezone.utc).isoformat()}})
    if result.modified_count == 0:
        raise HTTPException(status_code=404, detail="Fournisseur non trouvé")
    await log_action(current_user['id'], current_user['full_name'], "Archivage fournisseur", f"Fournisseur archivé: {fournisseur_id}")
    return {"message": "Fournisseur archivé"}

@api_router.delete("/fournisseurs/{fournisseur_id}")
async def delete_fournisseur(fournisseur_id: str, current_user: dict = Depends(get_current_user)):
    check_access(current_user, ["Super Admin"])
    result = await db.fournisseurs.delete_one({"id": fournisseur_id})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Fournisseur non trouvé")
    await log_action(current_user['id'], current_user['full_name'], "Suppression fournisseur", f"Fournisseur supprimé: {fournisseur_id}")
    return {"message": "Fournisseur supprimé"}

# ==================== DEVIS ROUTES ====================

@api_router.get("/devis", response_model=List[DevisResponse])
async def get_devis(include_archived: bool = False, current_user: dict = Depends(get_current_user)):
    query = {} if include_archived else {"is_archived": False}
    devis_list = await db.devis.find(query, {"_id": 0}).to_list(1000)
    return [DevisResponse(**d) for d in devis_list]

@api_router.post("/devis", response_model=DevisResponse)
async def create_devis(data: DevisCreate, current_user: dict = Depends(get_current_user)):
    fournisseur_nom = None
    if data.fournisseur_id:
        fournisseur = await db.fournisseurs.find_one({"id": data.fournisseur_id}, {"_id": 0})
        if fournisseur:
            fournisseur_nom = fournisseur.get("nom")
    
    devis = {
        "id": str(uuid.uuid4()),
        "titre": data.titre,
        "fournisseur_id": data.fournisseur_id,
        "fournisseur_nom": fournisseur_nom,
        "montant": data.montant,
        "description": data.description,
        "evenement": data.evenement,
        "statut": "En attente",
        "created_by": current_user['id'],
        "created_by_name": current_user['full_name'],
        "created_at": datetime.now(timezone.utc).isoformat(),
        "validated_by": None,
        "validated_at": None,
        "is_archived": False
    }
    await db.devis.insert_one(devis)
    await log_action(current_user['id'], current_user['full_name'], "Création devis", f"Devis créé: {data.titre}")
    fire_and_forget_email(COORDINATION_NOTIFY_EMAIL, "Nouveau devis en attente de validation", email_template(
        "Un nouveau devis attend une validation",
        ["Bonjour,", f"<b>Titre :</b> {data.titre}", f"<b>Montant :</b> {data.montant} €", f"<b>Demandé par :</b> {current_user['full_name']}"],
        kind="pending"
    ))
    coord_ids = await get_coordination_user_ids()
    await create_notification(
        coord_ids, "devis",
        "Nouveau devis en attente",
        f"{current_user['full_name']} a soumis un devis « {data.titre} » ({data.montant} €).",
        link="/devis"
    )
    return DevisResponse(**devis)

@api_router.put("/devis/{devis_id}/validate")
async def validate_devis(devis_id: str, current_user: dict = Depends(get_current_user)):
    await check_access_or_permission(current_user, ["Super Admin", "Responsable"], "devis.validate")
    devis = await db.devis.find_one({"id": devis_id}, {"_id": 0})
    if not devis:
        raise HTTPException(status_code=404, detail="Devis non trouvé")
    result = await db.devis.update_one(
        {"id": devis_id, "statut": "En attente"},
        {"$set": {"statut": "Validé", "validated_by": current_user['full_name'], "validated_at": datetime.now(timezone.utc).isoformat()}}
    )
    if result.modified_count == 0:
        raise HTTPException(status_code=404, detail="Devis non trouvé ou déjà traité")
    await log_action(current_user['id'], current_user['full_name'], "Validation devis", f"Devis validé: {devis_id}")
    fire_and_forget_email(await get_user_email(devis['created_by']), "Devis validé", email_template(
        "Votre devis a été validé",
        [f"Bonjour {devis.get('created_by_name', '')},", f"<b>Titre :</b> {devis['titre']}", f"<b>Montant :</b> {devis['montant']} €"],
        kind="success"
    ))
    return {"message": "Devis validé"}

@api_router.put("/devis/{devis_id}/reject")
async def reject_devis(devis_id: str, current_user: dict = Depends(get_current_user)):
    await check_access_or_permission(current_user, ["Super Admin", "Responsable"], "devis.validate")
    devis = await db.devis.find_one({"id": devis_id}, {"_id": 0})
    if not devis:
        raise HTTPException(status_code=404, detail="Devis non trouvé")
    result = await db.devis.update_one(
        {"id": devis_id, "statut": "En attente"},
        {"$set": {"statut": "Refusé", "validated_by": current_user['full_name'], "validated_at": datetime.now(timezone.utc).isoformat()}}
    )
    if result.modified_count == 0:
        raise HTTPException(status_code=404, detail="Devis non trouvé ou déjà traité")
    await log_action(current_user['id'], current_user['full_name'], "Refus devis", f"Devis refusé: {devis_id}")
    fire_and_forget_email(await get_user_email(devis['created_by']), "Devis refusé", email_template(
        "Votre devis a été refusé",
        [f"Bonjour {devis.get('created_by_name', '')},", f"<b>Titre :</b> {devis['titre']}", f"<b>Montant :</b> {devis['montant']} €"],
        kind="danger"
    ))
    return {"message": "Devis refusé"}

@api_router.put("/devis/{devis_id}/revert")
async def revert_devis(devis_id: str, current_user: dict = Depends(get_current_user)):
    """Send a Validé/Refusé devis back to 'En attente' so it can be re-reviewed."""
    await check_access_or_permission(current_user, ["Super Admin", "Responsable"], "devis.validate")
    devis = await db.devis.find_one({"id": devis_id}, {"_id": 0})
    result = await db.devis.update_one(
        {"id": devis_id, "statut": {"$in": ["Validé", "Refusé"]}},
        {"$set": {"statut": "En attente", "validated_by": None, "validated_at": None}}
    )
    if result.modified_count == 0:
        raise HTTPException(status_code=404, detail="Devis non trouvé ou déjà en attente")
    await log_action(current_user['id'], current_user['full_name'], "Retour devis en attente", f"Devis remis en attente: {devis_id}")
    if devis:
        fire_and_forget_email(await get_user_email(devis['created_by']), "Devis remis en attente", email_template(
            "Votre devis a été remis en attente de validation",
            [f"Bonjour {devis.get('created_by_name', '')},", f"<b>Titre :</b> {devis['titre']}"],
            kind="pending"
        ))
    return {"message": "Devis remis en attente"}

@api_router.put("/devis/{devis_id}")
async def update_devis(devis_id: str, data: DevisCreate, current_user: dict = Depends(get_current_user)):
    fournisseur_nom = None
    if data.fournisseur_id:
        fournisseur = await db.fournisseurs.find_one({"id": data.fournisseur_id}, {"_id": 0})
        if fournisseur:
            fournisseur_nom = fournisseur.get("nom")
    
    update_data = {
        "titre": data.titre,
        "fournisseur_id": data.fournisseur_id,
        "fournisseur_nom": fournisseur_nom,
        "montant": data.montant,
        "description": data.description,
        "evenement": data.evenement,
        "updated_at": datetime.now(timezone.utc).isoformat()
    }
    result = await db.devis.update_one({"id": devis_id}, {"$set": update_data})
    if result.modified_count == 0:
        raise HTTPException(status_code=404, detail="Devis non trouvé")
    await log_action(current_user['id'], current_user['full_name'], "Modification devis", f"Devis modifié: {data.titre}")
    devis = await db.devis.find_one({"id": devis_id}, {"_id": 0})
    return DevisResponse(**devis)

@api_router.put("/devis/{devis_id}/archive")
async def archive_devis(devis_id: str, current_user: dict = Depends(get_current_user)):
    await check_access_or_permission(current_user, ["Super Admin"], "devis.delete")
    result = await db.devis.update_one({"id": devis_id}, {"$set": {"is_archived": True}})
    if result.modified_count == 0:
        raise HTTPException(status_code=404, detail="Devis non trouvé")
    await log_action(current_user['id'], current_user['full_name'], "Archivage devis", f"Devis archivé: {devis_id}")
    return {"message": "Devis archivé"}

@api_router.delete("/devis/{devis_id}")
async def delete_devis(devis_id: str, current_user: dict = Depends(get_current_user)):
    check_access(current_user, ["Super Admin"])
    result = await db.devis.delete_one({"id": devis_id})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Devis non trouvé")
    await log_action(current_user['id'], current_user['full_name'], "Suppression devis", f"Devis supprimé: {devis_id}")
    return {"message": "Devis supprimé"}

# ==================== FORMATIONS ROUTES ====================

async def _formations_with_extras(query: dict) -> List[FormationResponse]:
    """Attaches interested_members (names of anyone who suggested/expressed
    interest in that formation, excluding rejected ones) to each formation."""
    formations = await db.formations.find(query, {"_id": 0}).to_list(1000)
    if not formations:
        return []
    ids = [f['id'] for f in formations]
    suggestions = await db.formation_suggestions.find(
        {"formation_id": {"$in": ids}, "statut": {"$ne": "Rejetée"}}, {"_id": 0}
    ).to_list(2000)
    interested_map = {}
    for s in suggestions:
        interested_map.setdefault(s['formation_id'], []).append(s['created_by_name'])
    for f in formations:
        f['interested_members'] = interested_map.get(f['id'], [])
    return [FormationResponse(**f) for f in formations]

@api_router.get("/formations", response_model=List[FormationResponse])
async def get_formations(include_archived: bool = False, current_user: dict = Depends(get_current_user)):
    query = {} if include_archived else {"is_archived": False}
    return await _formations_with_extras(query)

@api_router.post("/formations", response_model=FormationResponse)
async def create_formation(data: FormationCreate, current_user: dict = Depends(get_current_user)):
    formation = {
        "id": str(uuid.uuid4()),
        **data.model_dump(),
        "formateur": None,
        "cursus": None,
        "lieu": None,
        "statut": "En attente Coordination",
        "created_by": current_user['id'],
        "created_by_name": current_user['full_name'],
        "created_at": datetime.now(timezone.utc).isoformat(),
        "coordination_by": None,
        "coordination_at": None,
        "validated_by": None,
        "validated_at": None,
        "motif_refus": None,
        "refused_stage": None,
        "is_archived": False,
        "archived_at": None,
        "disponible_catalogue": False,
        "origine": "demande_membre" if current_user['niveau_acces'] == 'Technicien' else "proposition_responsable"
    }
    await db.formations.insert_one(formation)
    await log_action(current_user['id'], current_user['full_name'], "Demande formation", f"Formation demandée: {data.titre}")
    dates_str = ", ".join(data.dates_souhaitees) if data.dates_souhaitees else "À définir"
    fire_and_forget_email(COORDINATION_NOTIFY_EMAIL, "Nouvelle demande de formation", email_template(
        "Une nouvelle demande de formation attend la Coordination",
        ["Bonjour,", f"<b>Titre :</b> {data.titre}", f"<b>Demandé par :</b> {current_user['full_name']}", f"<b>Date(s) souhaitée(s) :</b> {dates_str}"],
        kind="pending"
    ))
    formation['interested_members'] = []
    return FormationResponse(**formation)

@api_router.put("/formations/{formation_id}", response_model=FormationResponse)
async def update_formation(formation_id: str, data: FormationCreate, current_user: dict = Depends(get_current_user)):
    existing = await db.formations.find_one({"id": formation_id}, {"_id": 0})
    if not existing:
        raise HTTPException(status_code=404, detail="Formation non trouvée")
    if existing["statut"] != "En attente Coordination":
        raise HTTPException(status_code=400, detail="Cette demande a déjà été traitée par la Coordination et ne peut plus être modifiée")
    update_data = {
        "titre": data.titre,
        "description": data.description,
        "dates_souhaitees": data.dates_souhaitees,
        "duree": data.duree,
        "updated_at": datetime.now(timezone.utc).isoformat()
    }
    await db.formations.update_one({"id": formation_id}, {"$set": update_data})
    await log_action(current_user['id'], current_user['full_name'], "Modification formation", f"Formation modifiée: {data.titre}")
    formation = await db.formations.find_one({"id": formation_id}, {"_id": 0})
    formation['interested_members'] = []
    return FormationResponse(**formation)

@api_router.put("/formations/{formation_id}/coordination-validate", response_model=FormationResponse)
async def coordination_validate_formation(formation_id: str, data: FormationCoordinationValidate, current_user: dict = Depends(get_current_user)):
    """Coordination confirms the trainer, curriculum and location, then
    transmits the request to Direction for final validation."""
    if not is_coordination_or_admin(current_user):
        raise HTTPException(status_code=403, detail="Réservé à la Coordination")
    update = {
        "formateur": data.formateur,
        "cursus": data.cursus,
        "lieu": data.lieu,
        "statut": "En attente validation finale",
        "coordination_by": current_user['full_name'],
        "coordination_at": datetime.now(timezone.utc).isoformat()
    }
    if data.duree:
        update["duree"] = data.duree
    result = await db.formations.update_one(
        {"id": formation_id, "statut": "En attente Coordination"},
        {"$set": update}
    )
    if result.modified_count == 0:
        raise HTTPException(status_code=404, detail="Formation non trouvée ou déjà traitée")
    await log_action(current_user['id'], current_user['full_name'], "Validation Coordination", f"Formation transmise pour validation finale: {formation_id}")
    formation = await db.formations.find_one({"id": formation_id}, {"_id": 0})
    fire_and_forget_email(await get_user_email(formation['created_by']), "Votre demande de formation avance", email_template(
        "Votre demande de formation est transmise pour validation finale",
        [f"Bonjour {formation.get('created_by_name', '')},", f"<b>Titre :</b> {formation['titre']}",
         f"<b>Formateur :</b> {formation.get('formateur') or '-'}", f"<b>Lieu :</b> {formation.get('lieu') or '-'}"],
        kind="pending"
    ))
    fire_and_forget_email(COORDINATION_NOTIFY_EMAIL, "Formation en attente de validation finale", email_template(
        "Une formation attend la validation finale de la Direction",
        ["Bonjour,", f"<b>Titre :</b> {formation['titre']}"],
        kind="pending"
    ))
    return FormationResponse(**formation)

@api_router.put("/formations/{formation_id}/coordination-reject", response_model=FormationResponse)
async def coordination_reject_formation(formation_id: str, data: FormationRejectReason, current_user: dict = Depends(get_current_user)):
    if not is_coordination_or_admin(current_user):
        raise HTTPException(status_code=403, detail="Réservé à la Coordination")
    result = await db.formations.update_one(
        {"id": formation_id, "statut": "En attente Coordination"},
        {"$set": {
            "statut": "Refusée",
            "refused_stage": "coordination",
            "motif_refus": data.motif,
            "coordination_by": current_user['full_name'],
            "coordination_at": datetime.now(timezone.utc).isoformat()
        }}
    )
    if result.modified_count == 0:
        raise HTTPException(status_code=404, detail="Formation non trouvée ou déjà traitée")
    await log_action(current_user['id'], current_user['full_name'], "Refus formation (Coordination)", f"Formation refusée: {formation_id}")
    formation = await db.formations.find_one({"id": formation_id}, {"_id": 0})
    fire_and_forget_email(await get_user_email(formation['created_by']), "Demande de formation refusée", email_template(
        "Votre demande de formation a été refusée",
        [f"Bonjour {formation.get('created_by_name', '')},", f"<b>Titre :</b> {formation['titre']}", f"<b>Motif :</b> {data.motif or '-'}"],
        kind="danger"
    ))
    return FormationResponse(**formation)

@api_router.put("/formations/{formation_id}/final-validate", response_model=FormationResponse)
async def final_validate_formation(formation_id: str, current_user: dict = Depends(get_current_user)):
    """Final validation by Direction (e.g. Paul) or Admin/Super Admin."""
    if not is_direction_or_admin(current_user):
        raise HTTPException(status_code=403, detail="Réservé à la Direction")
    result = await db.formations.update_one(
        {"id": formation_id, "statut": "En attente validation finale"},
        {"$set": {
            "statut": "Validée",
            "validated_by": current_user['full_name'],
            "validated_at": datetime.now(timezone.utc).isoformat()
        }}
    )
    if result.modified_count == 0:
        raise HTTPException(status_code=404, detail="Formation non trouvée ou déjà traitée")
    await log_action(current_user['id'], current_user['full_name'], "Validation finale formation", f"Formation validée: {formation_id}")
    formation = await db.formations.find_one({"id": formation_id}, {"_id": 0})
    fire_and_forget_email(await get_user_email(formation['created_by']), "Formation validée", email_template(
        "Votre demande de formation a été validée",
        [f"Bonjour {formation.get('created_by_name', '')},", f"<b>Titre :</b> {formation['titre']}", f"<b>Date(s) souhaitée(s) :</b> {', '.join(formation.get('dates_souhaitees') or []) or '-'}"],
        kind="success"
    ))
    return FormationResponse(**formation)

@api_router.put("/formations/{formation_id}/final-reject", response_model=FormationResponse)
async def final_reject_formation(formation_id: str, data: FormationRejectReason, current_user: dict = Depends(get_current_user)):
    if not is_direction_or_admin(current_user):
        raise HTTPException(status_code=403, detail="Réservé à la Direction")
    result = await db.formations.update_one(
        {"id": formation_id, "statut": "En attente validation finale"},
        {"$set": {
            "statut": "Refusée",
            "refused_stage": "direction",
            "motif_refus": data.motif,
            "validated_by": current_user['full_name'],
            "validated_at": datetime.now(timezone.utc).isoformat()
        }}
    )
    if result.modified_count == 0:
        raise HTTPException(status_code=404, detail="Formation non trouvée ou déjà traitée")
    await log_action(current_user['id'], current_user['full_name'], "Refus formation (Direction)", f"Formation refusée: {formation_id}")
    formation = await db.formations.find_one({"id": formation_id}, {"_id": 0})
    fire_and_forget_email(await get_user_email(formation['created_by']), "Demande de formation refusée", email_template(
        "Votre demande de formation a été refusée en validation finale",
        [f"Bonjour {formation.get('created_by_name', '')},", f"<b>Titre :</b> {formation['titre']}", f"<b>Motif :</b> {data.motif or '-'}"],
        kind="danger"
    ))
    return FormationResponse(**formation)

@api_router.get("/formations/catalogue", response_model=List[FormationResponse])
async def get_formations_catalogue(current_user: dict = Depends(get_current_user)):
    """Public-to-all-users list of formations Coordination has marked as
    available in the catalogue — what Membres can browse from Mon espace."""
    formations = await db.formations.find(
        {"is_archived": False, "disponible_catalogue": True}, {"_id": 0}
    ).to_list(1000)
    return [FormationResponse(**f) for f in formations]

@api_router.put("/formations/{formation_id}/catalogue", response_model=FormationResponse)
async def toggle_formation_catalogue(formation_id: str, data: FormationCatalogueToggle, current_user: dict = Depends(get_current_user)):
    if not is_coordination_or_admin(current_user):
        raise HTTPException(status_code=403, detail="Réservé à la Coordination")
    result = await db.formations.update_one({"id": formation_id}, {"$set": {"disponible_catalogue": data.disponible_catalogue}})
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Formation non trouvée")
    await log_action(current_user['id'], current_user['full_name'], "Catalogue formation",
                      f"Formation {'ajoutée au' if data.disponible_catalogue else 'retirée du'} catalogue: {formation_id}")
    formation = await db.formations.find_one({"id": formation_id}, {"_id": 0})
    return FormationResponse(**formation)

# ==================== FORMATION SUGGESTIONS (Membre-facing) ====================

@api_router.post("/formation-suggestions", response_model=FormationSuggestionResponse)
async def create_formation_suggestion(data: FormationSuggestionCreate, current_user: dict = Depends(get_current_user)):
    """Any authenticated user (Membre included) can suggest a new formation
    topic, or express interest in an existing catalogue formation by
    passing formation_id."""
    suggestion = {
        "id": str(uuid.uuid4()),
        **data.model_dump(),
        "statut": "En attente",
        "created_by": current_user['id'],
        "created_by_name": current_user['full_name'],
        "created_at": datetime.now(timezone.utc).isoformat(),
        "resulting_formation_id": None,
        "is_archived": False,
        "archived_at": None,
    }
    await db.formation_suggestions.insert_one(suggestion)
    await log_action(current_user['id'], current_user['full_name'], "Suggestion formation", f"Suggestion: {data.titre}")
    coord_ids = await get_coordination_user_ids()
    if data.formation_id:
        # Interest expressed in an existing catalogue formation — notify Gestionnaire+/Coordination.
        fire_and_forget_email(COORDINATION_NOTIFY_EMAIL, "Nouvel intérêt pour une formation", email_template(
            "Un membre a manifesté son intérêt pour une formation du catalogue",
            ["Bonjour,", f"<b>Formation :</b> {data.titre}", f"<b>Membre intéressé :</b> {current_user['full_name']}"],
            kind="pending"
        ))
        await create_notification(
            coord_ids, "formation_interet",
            "Intérêt pour une formation",
            f"{current_user['full_name']} est intéressé(e) par la formation « {data.titre} ».",
            link="/formations"
        )
    else:
        await create_notification(
            coord_ids, "formation_suggestion",
            "Nouvelle suggestion de formation",
            f"{current_user['full_name']} propose un nouveau sujet de formation : « {data.titre} ».",
            link="/formations"
        )
    return FormationSuggestionResponse(**suggestion)

@api_router.get("/formation-suggestions", response_model=List[FormationSuggestionResponse])
async def get_formation_suggestions(include_archived: bool = False, current_user: dict = Depends(get_current_user)):
    await check_access_or_permission(current_user, ["Super Admin", "Admin", "Responsable", "Gestionnaire"], "formations.validate")
    query = {} if include_archived else {"is_archived": {"$ne": True}}
    suggestions = await db.formation_suggestions.find(query, {"_id": 0}).sort("created_at", -1).to_list(1000)
    return [FormationSuggestionResponse(**s) for s in suggestions]

@api_router.get("/formation-suggestions/mine", response_model=List[FormationSuggestionResponse])
async def get_my_formation_suggestions(include_archived: bool = False, current_user: dict = Depends(get_current_user)):
    query = {"created_by": current_user['id']}
    if not include_archived:
        query["is_archived"] = {"$ne": True}
    suggestions = await db.formation_suggestions.find(query, {"_id": 0}).sort("created_at", -1).to_list(1000)
    return [FormationSuggestionResponse(**s) for s in suggestions]

@api_router.put("/formation-suggestions/{suggestion_id}/archive")
async def archive_formation_suggestion(suggestion_id: str, current_user: dict = Depends(get_current_user)):
    """A suggestion's creator or Coordination/Admin can archive it — keeps
    the Suggestions tab tidy without permanently deleting the record.
    Archived suggestions are purged after 6 months, same as formations."""
    suggestion = await db.formation_suggestions.find_one({"id": suggestion_id}, {"_id": 0})
    if not suggestion:
        raise HTTPException(status_code=404, detail="Suggestion non trouvée")
    if suggestion['created_by'] != current_user['id'] and not is_coordination_or_admin(current_user):
        raise HTTPException(status_code=403, detail="Non autorisé")
    await db.formation_suggestions.update_one(
        {"id": suggestion_id},
        {"$set": {"is_archived": True, "archived_at": datetime.now(timezone.utc).isoformat()}}
    )
    await log_action(current_user['id'], current_user['full_name'], "Archivage suggestion formation", f"Suggestion archivée: {suggestion.get('titre')}")
    return {"message": "Suggestion archivée"}

@api_router.put("/formation-suggestions/{suggestion_id}/unarchive")
async def unarchive_formation_suggestion(suggestion_id: str, current_user: dict = Depends(get_current_user)):
    await check_access_or_permission(current_user, ["Super Admin", "Responsable", "Gestionnaire"], "formations.write")
    result = await db.formation_suggestions.update_one(
        {"id": suggestion_id},
        {"$set": {"is_archived": False, "archived_at": None}}
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Suggestion non trouvée")
    await log_action(current_user['id'], current_user['full_name'], "Désarchivage suggestion formation", suggestion_id)
    return {"message": "Suggestion désarchivée"}

@api_router.delete("/formation-suggestions/{suggestion_id}")
async def withdraw_formation_suggestion(suggestion_id: str, current_user: dict = Depends(get_current_user)):
    """A member can withdraw their own suggestion or expressed interest.
    If it's tied to a scheduled formation, withdrawal is blocked within 72h
    of the earliest requested date."""
    suggestion = await db.formation_suggestions.find_one({"id": suggestion_id}, {"_id": 0})
    if not suggestion:
        raise HTTPException(status_code=404, detail="Suggestion non trouvée")
    if suggestion['created_by'] != current_user['id'] and not is_coordination_or_admin(current_user):
        raise HTTPException(status_code=403, detail="Non autorisé")
    if suggestion.get('formation_id'):
        formation = await db.formations.find_one({"id": suggestion['formation_id']}, {"_id": 0})
        dates = (formation or {}).get('dates_souhaitees') or []
        if dates:
            try:
                earliest = min(datetime.fromisoformat(d).replace(tzinfo=timezone.utc) for d in dates)
                deadline = earliest - timedelta(hours=72)
                if datetime.now(timezone.utc) > deadline:
                    raise HTTPException(status_code=400, detail="Trop tard pour se retirer (moins de 72h avant la formation)")
            except ValueError:
                pass  # malformed date, don't block withdrawal over it
    await db.formation_suggestions.delete_one({"id": suggestion_id})
    await log_action(current_user['id'], current_user['full_name'], "Retrait suggestion formation", f"Suggestion retirée: {suggestion.get('titre')}")
    return {"message": "Retrait effectué"}

@api_router.put("/formation-suggestions/{suggestion_id}/status", response_model=FormationSuggestionResponse)
async def update_formation_suggestion_status(suggestion_id: str, data: FormationSuggestionStatusUpdate, current_user: dict = Depends(get_current_user)):
    await check_access_or_permission(current_user, ["Super Admin", "Responsable", "Gestionnaire"], "formations.validate")
    suggestion = await db.formation_suggestions.find_one({"id": suggestion_id}, {"_id": 0})
    if not suggestion:
        raise HTTPException(status_code=404, detail="Suggestion non trouvée")
    update_fields = {"statut": data.statut}
    # Approving a brand-new topic suggestion (no formation_id yet, i.e. not
    # "interest in an existing catalogue item") turns it into a real formation
    # request so it actually enters the Coordination -> Direction pipeline
    # instead of just sitting there approved with nothing happening.
    if data.statut == "Approuvée" and not suggestion.get('formation_id') and not suggestion.get('resulting_formation_id'):
        new_formation = {
            "id": str(uuid.uuid4()),
            "titre": suggestion['titre'],
            "description": suggestion.get('description') or '',
            "dates_souhaitees": [],
            "duree": "À définir",
            "formateur": None,
            "cursus": None,
            "lieu": None,
            "statut": "En attente Coordination",
            "created_by": suggestion['created_by'],
            "created_by_name": suggestion['created_by_name'],
            "created_at": datetime.now(timezone.utc).isoformat(),
            "coordination_by": None,
            "coordination_at": None,
            "validated_by": None,
            "validated_at": None,
            "motif_refus": None,
            "refused_stage": None,
            "is_archived": False,
            "archived_at": None,
            "disponible_catalogue": False,
            "origine": "suggestion_membre"
        }
        await db.formations.insert_one(new_formation)
        update_fields["resulting_formation_id"] = new_formation["id"]
        await log_action(current_user['id'], current_user['full_name'], "Suggestion → formation", f"Suggestion transformée en demande: {suggestion['titre']}")
    result = await db.formation_suggestions.update_one({"id": suggestion_id}, {"$set": update_fields})
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Suggestion non trouvée")
    await log_action(current_user['id'], current_user['full_name'], "Suggestion formation", f"Statut mis à jour: {data.statut}")
    s = await db.formation_suggestions.find_one({"id": suggestion_id}, {"_id": 0})
    return FormationSuggestionResponse(**s)

@api_router.put("/formations/{formation_id}/archive")
async def archive_formation(formation_id: str, current_user: dict = Depends(get_current_user)):
    await check_access_or_permission(current_user, ["Super Admin"], "formations.delete")
    result = await db.formations.update_one(
        {"id": formation_id},
        {"$set": {"is_archived": True, "archived_at": datetime.now(timezone.utc).isoformat()}}
    )
    if result.modified_count == 0:
        raise HTTPException(status_code=404, detail="Formation non trouvée")
    await log_action(current_user['id'], current_user['full_name'], "Archivage formation", f"Formation archivée: {formation_id}")
    return {"message": "Formation archivée"}

async def purge_old_archived_formations():
    """Archived formations and formation suggestions are kept 6 months for
    reference, then purged. Runs once at startup and then every 24h."""
    while True:
        try:
            cutoff = (datetime.now(timezone.utc) - timedelta(days=180)).isoformat()
            await db.formations.delete_many({"is_archived": True, "archived_at": {"$lt": cutoff}})
            await db.formation_suggestions.delete_many({"is_archived": True, "archived_at": {"$lt": cutoff}})
        except Exception as e:
            logger.error(f"Purge formations/suggestions archivées échouée: {e}")
        await asyncio.sleep(24 * 60 * 60)

@api_router.delete("/formations/{formation_id}")
async def delete_formation(formation_id: str, current_user: dict = Depends(get_current_user)):
    check_access(current_user, ["Super Admin"])
    result = await db.formations.delete_one({"id": formation_id})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Formation non trouvée")
    await log_action(current_user['id'], current_user['full_name'], "Suppression formation", f"Formation supprimée: {formation_id}")
    return {"message": "Formation supprimée"}

# ==================== PLANNING ROUTES ====================

@api_router.get("/planning", response_model=List[PlanningResponse])
async def get_plannings(include_archived: bool = False, current_user: dict = Depends(get_current_user)):
    query = {} if include_archived else {"is_archived": False}
    plannings = await db.planning.find(query, {"_id": 0}).sort([("annee", -1), ("mois", -1)]).to_list(100)
    return [PlanningResponse(**p) for p in plannings]

@api_router.get("/planning/{annee}/{mois}", response_model=PlanningResponse)
async def get_planning_by_month(annee: int, mois: int, current_user: dict = Depends(get_current_user)):
    planning = await db.planning.find_one({"annee": annee, "mois": mois}, {"_id": 0})
    if not planning:
        raise HTTPException(status_code=404, detail="Planning non trouvé")
    return PlanningResponse(**planning)

class PlanningScopeResponse(BaseModel):
    is_restricted: bool  # True only for a Responsable without planning_full_control
    full_control: bool
    scope: List[str]

@api_router.get("/me/planning-scope", response_model=PlanningScopeResponse)
async def get_my_planning_scope(current_user: dict = Depends(get_current_user)):
    """Drives frontend cell-level gating in Planning.js. Only the Responsable
    role can be restricted — every other role that reaches the Planning
    write UI already has unrestricted access via the role hierarchy."""
    if current_user['niveau_acces'] != 'Responsable':
        return PlanningScopeResponse(is_restricted=False, full_control=True, scope=[])
    full_control, scope = await get_user_planning_scope(current_user)
    return PlanningScopeResponse(is_restricted=not full_control, full_control=full_control, scope=sorted(scope))

@api_router.post("/planning", response_model=PlanningResponse)
async def create_planning(data: PlanningCreate, current_user: dict = Depends(get_current_user)):
    # Gestionnaire is included so they can save the Absences/Notes fields they're
    # allowed to edit in the web Planning; the UI itself keeps the affectation
    # grid cells read-only for anyone below Responsable.
    await check_access_or_permission(current_user, ["Super Admin", "Responsable", "Gestionnaire"], "planning.write")
    if current_user['niveau_acces'] == 'Responsable':
        full_control, _scope = await get_user_planning_scope(current_user)
        if not full_control:
            raise HTTPException(status_code=403, detail="Seuls les groupes à contrôle total peuvent créer un nouveau planning")
    existing = await db.planning.find_one({"annee": data.annee, "mois": data.mois, "is_archived": False})
    if existing:
        raise HTTPException(status_code=400, detail="Un planning existe déjà pour ce mois")
    
    planning = {
        "id": str(uuid.uuid4()),
        "mois": data.mois,
        "annee": data.annee,
        "dates": data.dates,
        "affectations": data.affectations,
        "sections": data.sections,
        "notes": data.notes,
        "absences": data.absences,
        "blocked_cells": data.blocked_cells,
        "titre_overrides": data.titre_overrides,
        "date_labels": data.date_labels,
        "is_archived": False,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat()
    }
    await db.planning.insert_one(planning)
    await log_action(current_user['id'], current_user['full_name'], "Création planning", f"Planning créé: {data.mois}/{data.annee}")
    return PlanningResponse(**planning)

@api_router.put("/planning/{planning_id}", response_model=PlanningResponse)
async def update_planning(planning_id: str, data: PlanningCreate, current_user: dict = Depends(get_current_user)):
    # Same rationale as create_planning above: Gestionnaire needs to be able
    # to save after editing Absences/Notes.
    await check_access_or_permission(current_user, ["Super Admin", "Responsable", "Gestionnaire"], "planning.write")
    if current_user['niveau_acces'] == 'Responsable':
        full_control, scope = await get_user_planning_scope(current_user)
        if not full_control:
            existing_check = await db.planning.find_one({"id": planning_id}, {"_id": 0})
            if not existing_check:
                raise HTTPException(status_code=404, detail="Planning non trouvé")
            # A scoped Responsable may only fill in affectation values within
            # their group's sections/postes — structural edits (categories,
            # dates, blocked cells, custom titles/labels) require full control.
            structural_fields = ["sections", "dates", "blocked_cells", "titre_overrides", "date_labels"]
            for field in structural_fields:
                if getattr(data, field) != existing_check.get(field):
                    raise HTTPException(status_code=403, detail="Votre groupe ne permet pas de modifier la structure du planning (catégories, dates, libellés) — seulement les affectations de votre périmètre")
            role_section = build_role_section_map(existing_check.get('sections') or {})
            old_aff = existing_check.get('affectations') or {}
            new_aff = data.affectations or {}
            for key in set(list(old_aff.keys()) + list(new_aff.keys())):
                if old_aff.get(key) == new_aff.get(key):
                    continue
                section_name = role_section.get(key, '')
                allowed = (section_name and section_name in scope) or any(p and p in key for p in scope)
                if not allowed:
                    raise HTTPException(status_code=403, detail=f"Vous n'avez pas les droits sur « {section_name or key} »")
    update_data = {
        "dates": data.dates,
        "affectations": data.affectations,
        "sections": data.sections,
        "notes": data.notes,
        "absences": data.absences,
        "blocked_cells": data.blocked_cells,
        "titre_overrides": data.titre_overrides,
        "date_labels": data.date_labels,
        "updated_at": datetime.now(timezone.utc).isoformat()
    }
    result = await db.planning.update_one({"id": planning_id}, {"$set": update_data})
    if result.modified_count == 0:
        raise HTTPException(status_code=404, detail="Planning non trouvé")
    planning = await db.planning.find_one({"id": planning_id}, {"_id": 0})
    await log_action(current_user['id'], current_user['full_name'], "Modification planning", f"Planning modifié: {data.mois}/{data.annee}")
    return PlanningResponse(**planning)

@api_router.put("/planning/{planning_id}/archive")
async def archive_planning(planning_id: str, current_user: dict = Depends(get_current_user)):
    await check_access_or_permission(current_user, ["Super Admin"], "planning.delete")
    result = await db.planning.update_one({"id": planning_id}, {"$set": {"is_archived": True, "updated_at": datetime.now(timezone.utc).isoformat()}})
    if result.modified_count == 0:
        raise HTTPException(status_code=404, detail="Planning non trouvé")
    await log_action(current_user['id'], current_user['full_name'], "Archivage planning", f"Planning archivé: {planning_id}")
    return {"message": "Planning archivé"}

# ==================== ABSENCES ROUTES ====================
# Self-service absence declarations. Any authenticated user can declare their
# own absence (date range + reason); Gestionnaire+ can see everyone's
# declarations to plan around them (their own view is scoped to their
# assigned branch(es), same as the Dashboard/Effectif restriction).

def _dates_overlap(a_start: str, a_end: str, b_start: str, b_end: str) -> bool:
    return a_start <= b_end and b_start <= a_end

@api_router.post("/absences", response_model=AbsenceResponse)
async def create_absence(data: AbsenceCreate, current_user: dict = Depends(get_current_user)):
    if data.date_fin < data.date_debut:
        raise HTTPException(status_code=400, detail="La date de fin doit être après la date de début")
    absence = {
        "id": str(uuid.uuid4()),
        "user_id": current_user["id"],
        "full_name": current_user["full_name"],
        "date_debut": data.date_debut,
        "date_fin": data.date_fin,
        "raison": data.raison,
        "created_at": datetime.now(timezone.utc).isoformat()
    }
    await db.absences.insert_one(absence)
    await log_action(current_user['id'], current_user['full_name'], "Déclaration absence", f"{data.date_debut} → {data.date_fin}: {data.raison}")
    gestion_ids = await get_user_ids_by_roles(["Super Admin", "Admin", "Responsable", "Gestionnaire"])
    await create_notification(
        gestion_ids, "absence",
        "Nouvelle absence déclarée",
        f"{current_user['full_name']} sera absent(e) du {data.date_debut} au {data.date_fin} ({data.raison}).",
        link="/planning"
    )
    return AbsenceResponse(**{k: v for k, v in absence.items() if k != "_id"})

@api_router.get("/absences/mine", response_model=List[AbsenceResponse])
async def get_my_absences(current_user: dict = Depends(get_current_user)):
    absences = await db.absences.find({"user_id": current_user["id"]}, {"_id": 0}).sort("date_debut", 1).to_list(1000)
    return [AbsenceResponse(**a) for a in absences]

@api_router.get("/absences", response_model=List[AbsenceResponse])
async def get_absences(mois: Optional[int] = None, annee: Optional[int] = None, current_user: dict = Depends(get_current_user)):
    check_access(current_user, ["Super Admin", "Admin", "Responsable", "Gestionnaire"])
    absences = await db.absences.find({}, {"_id": 0}).sort("date_debut", 1).to_list(2000)

    if mois and annee:
        month_start = f"{annee:04d}-{mois:02d}-01"
        last_day = 31
        while True:
            try:
                datetime.strptime(f"{annee:04d}-{mois:02d}-{last_day:02d}", "%Y-%m-%d")
                break
            except ValueError:
                last_day -= 1
        month_end = f"{annee:04d}-{mois:02d}-{last_day:02d}"
        absences = [a for a in absences if _dates_overlap(a["date_debut"], a["date_fin"], month_start, month_end)]

    # Gestionnaire/Responsable scoped to their own branch(es) only see
    # absences declared by users sharing one of those branches; Admin/Super
    # Admin (and Responsables with no branch assigned) see everyone.
    is_admin = current_user["niveau_acces"] in ["Super Admin", "Admin"]
    my_branches = current_user.get("branches", [])
    if not is_admin and my_branches:
        user_ids = [a["user_id"] for a in absences]
        users = await db.users.find({"id": {"$in": user_ids}}, {"_id": 0, "id": 1, "branches": 1}).to_list(2000)
        branches_by_user = {u["id"]: set(u.get("branches", [])) for u in users}
        absences = [a for a in absences if branches_by_user.get(a["user_id"], set()) & set(my_branches)]

    return [AbsenceResponse(**a) for a in absences]

@api_router.delete("/absences/{absence_id}")
async def delete_absence(absence_id: str, current_user: dict = Depends(get_current_user)):
    absence = await db.absences.find_one({"id": absence_id}, {"_id": 0})
    if not absence:
        raise HTTPException(status_code=404, detail="Absence non trouvée")
    is_owner = absence["user_id"] == current_user["id"]
    is_manager = current_user["niveau_acces"] in ["Super Admin", "Admin", "Responsable", "Gestionnaire"]
    if not is_owner and not is_manager:
        raise HTTPException(status_code=403, detail="Accès refusé")
    await db.absences.delete_one({"id": absence_id})
    await log_action(current_user['id'], current_user['full_name'], "Suppression absence", absence_id)
    return {"message": "Absence supprimée"}

# ==================== ACTUALITES ROUTES ====================

@api_router.get("/actualites", response_model=List[ActualiteResponse])
async def get_actualites(current_user: dict = Depends(get_current_user)):
    actualites = await db.actualites.find({"is_active": True}, {"_id": 0}).sort("created_at", -1).to_list(100)
    return [ActualiteResponse(**a) for a in actualites]

@api_router.get("/actualites/public")
async def get_actualites_public():
    """Public endpoint for login page - no auth required"""
    actualites = await db.actualites.find({"is_active": True}, {"_id": 0}).sort("created_at", -1).limit(5).to_list(5)
    return actualites

@api_router.post("/actualites", response_model=ActualiteResponse)
async def create_actualite(data: ActualiteCreate, current_user: dict = Depends(get_current_user)):
    await check_access_or_permission(current_user, ["Super Admin", "Responsable", "Gestionnaire"], "actualites.write")
    actualite = {
        "id": str(uuid.uuid4()),
        "titre": data.titre,
        "description": data.description,
        "date_evenement": data.date_evenement,
        "image_url": data.image_url,
        "created_by": current_user['id'],
        "created_by_name": current_user['full_name'],
        "created_at": datetime.now(timezone.utc).isoformat(),
        "is_active": True
    }
    await db.actualites.insert_one(actualite)
    await log_action(current_user['id'], current_user['full_name'], "Création actualité", f"Actualité créée: {data.titre}")
    return ActualiteResponse(**actualite)

@api_router.put("/actualites/{actualite_id}", response_model=ActualiteResponse)
async def update_actualite(actualite_id: str, data: ActualiteCreate, current_user: dict = Depends(get_current_user)):
    await check_access_or_permission(current_user, ["Super Admin", "Responsable", "Gestionnaire"], "actualites.write")
    update_data = {
        "titre": data.titre,
        "description": data.description,
        "date_evenement": data.date_evenement,
        "image_url": data.image_url,
        "updated_at": datetime.now(timezone.utc).isoformat()
    }
    result = await db.actualites.update_one({"id": actualite_id}, {"$set": update_data})
    if result.modified_count == 0:
        raise HTTPException(status_code=404, detail="Actualité non trouvée")
    await log_action(current_user['id'], current_user['full_name'], "Modification actualité", f"Actualité modifiée: {data.titre}")
    actualite = await db.actualites.find_one({"id": actualite_id}, {"_id": 0})
    return ActualiteResponse(**actualite)

@api_router.delete("/actualites/{actualite_id}")
async def delete_actualite(actualite_id: str, current_user: dict = Depends(get_current_user)):
    check_access(current_user, ["Super Admin"])
    result = await db.actualites.delete_one({"id": actualite_id})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Actualité non trouvée")
    await log_action(current_user['id'], current_user['full_name'], "Suppression actualité", f"Actualité supprimée: {actualite_id}")
    return {"message": "Actualité supprimée"}

# ==================== DOCUMENTS ROUTES ====================

@api_router.get("/documents/categories", response_model=List[DocumentCategoryResponse])
async def get_document_categories(current_user: dict = Depends(get_current_user)):
    categories = await db.document_categories.find({}, {"_id": 0}).sort("nom", 1).to_list(100)
    return [DocumentCategoryResponse(**c) for c in categories]

@api_router.post("/documents/categories", response_model=DocumentCategoryResponse)
async def create_document_category(data: DocumentCategoryCreate, current_user: dict = Depends(get_current_user)):
    check_access(current_user, ["Super Admin"])
    category = {
        "id": str(uuid.uuid4()),
        "nom": data.nom,
        "description": data.description,
        "created_at": datetime.now(timezone.utc).isoformat()
    }
    await db.document_categories.insert_one(category)
    await log_action(current_user['id'], current_user['full_name'], "Création catégorie document", f"Catégorie créée: {data.nom}")
    return DocumentCategoryResponse(**category)

@api_router.put("/documents/categories/{category_id}", response_model=DocumentCategoryResponse)
async def update_document_category(category_id: str, data: DocumentCategoryCreate, current_user: dict = Depends(get_current_user)):
    check_access(current_user, ["Super Admin"])
    update_data = {"nom": data.nom, "description": data.description}
    result = await db.document_categories.update_one({"id": category_id}, {"$set": update_data})
    if result.modified_count == 0:
        raise HTTPException(status_code=404, detail="Catégorie non trouvée")
    category = await db.document_categories.find_one({"id": category_id}, {"_id": 0})
    return DocumentCategoryResponse(**category)

@api_router.delete("/documents/categories/{category_id}")
async def delete_document_category(category_id: str, current_user: dict = Depends(get_current_user)):
    check_access(current_user, ["Super Admin"])
    # Check if category has documents
    doc_count = await db.documents.count_documents({"categorie_id": category_id})
    if doc_count > 0:
        raise HTTPException(status_code=400, detail=f"Impossible de supprimer: {doc_count} document(s) dans cette catégorie")
    result = await db.document_categories.delete_one({"id": category_id})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Catégorie non trouvée")
    return {"message": "Catégorie supprimée"}

@api_router.get("/documents", response_model=List[DocumentResponse])
async def get_documents(current_user: dict = Depends(get_current_user)):
    documents = await db.documents.find({}, {"_id": 0}).sort("created_at", -1).to_list(500)
    # Get category names
    categories = {c['id']: c['nom'] for c in await db.document_categories.find({}, {"_id": 0, "id": 1, "nom": 1}).to_list(100)}
    user_role = current_user['niveau_acces']
    visible_documents = []
    for doc in documents:
        doc['categorie_nom'] = categories.get(doc.get('categorie_id'), 'Sans catégorie')
        visible_roles = doc.get('visible_roles') or []
        # Liste vide/absente = document public (visible par tous). Liste non vide =
        # restreint aux rôles listés ; Admin/Super Admin voient toujours tout, y
        # compris pour gérer les documents restreints depuis l'UI.
        if visible_roles and user_role not in visible_roles and user_role not in ("Super Admin", "Admin"):
            continue
        visible_documents.append(doc)
    return [DocumentResponse(**d) for d in visible_documents]

@api_router.post("/documents", response_model=DocumentResponse)
async def create_document(data: DocumentCreate, current_user: dict = Depends(get_current_user)):
    await check_access_or_permission(current_user, ["Super Admin", "Responsable", "Gestionnaire"], "documents.write")
    # Get category name
    category = await db.document_categories.find_one({"id": data.categorie_id}, {"_id": 0})
    categorie_nom = category['nom'] if category else 'Sans catégorie'

    document = {
        "id": str(uuid.uuid4()),
        "titre": data.titre,
        "categorie_id": data.categorie_id,
        "categorie_nom": categorie_nom,
        "description": data.description,
        "file_url": data.file_url,
        "file_type": data.file_type,
        "visible_roles": data.visible_roles or [],
        "created_by": current_user['id'],
        "created_by_name": current_user['full_name'],
        "created_at": datetime.now(timezone.utc).isoformat()
    }
    await db.documents.insert_one(document)
    await log_action(current_user['id'], current_user['full_name'], "Ajout document", f"Document ajouté: {data.titre}")
    return DocumentResponse(**document)

@api_router.put("/documents/{document_id}", response_model=DocumentResponse)
async def update_document(document_id: str, data: DocumentCreate, current_user: dict = Depends(get_current_user)):
    await check_access_or_permission(current_user, ["Super Admin", "Responsable", "Gestionnaire"], "documents.write")
    category = await db.document_categories.find_one({"id": data.categorie_id}, {"_id": 0})
    categorie_nom = category['nom'] if category else 'Sans catégorie'

    update_data = {
        "titre": data.titre,
        "categorie_id": data.categorie_id,
        "categorie_nom": categorie_nom,
        "description": data.description,
        "file_url": data.file_url,
        "file_type": data.file_type,
        "visible_roles": data.visible_roles or []
    }
    result = await db.documents.update_one({"id": document_id}, {"$set": update_data})
    if result.modified_count == 0:
        raise HTTPException(status_code=404, detail="Document non trouvé")
    document = await db.documents.find_one({"id": document_id}, {"_id": 0})
    return DocumentResponse(**document)

@api_router.delete("/documents/{document_id}")
async def delete_document(document_id: str, current_user: dict = Depends(get_current_user)):
    check_access(current_user, ["Super Admin"])
    result = await db.documents.delete_one({"id": document_id})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Document non trouvé")
    await log_action(current_user['id'], current_user['full_name'], "Suppression document", f"Document supprimé: {document_id}")
    return {"message": "Document supprimé"}

# ==================== DASHBOARD STATS ====================

@api_router.get("/dashboard/stats")
async def get_dashboard_stats(current_user: dict = Depends(get_current_user)):
    total_techniciens = await db.techniciens.count_documents({"is_archived": False})
    badges_attribues = await db.techniciens.count_documents({"is_archived": False, "badge_attribue": True})
    total_materiel = await db.materiel.count_documents({"is_archived": False})
    materiel_disponible = await db.materiel.count_documents({"is_archived": False, "statut": "Disponible"})
    devis_en_attente = await db.devis.count_documents({"is_archived": False, "statut": "En attente"})
    formations_en_attente_coordination = await db.formations.count_documents({"is_archived": False, "statut": "En attente Coordination"})
    formations_en_attente_validation_finale = await db.formations.count_documents({"is_archived": False, "statut": "En attente validation finale"})
    formations_en_attente = formations_en_attente_coordination + formations_en_attente_validation_finale
    
    # Stats Salles
    total_salles = await db.salles.count_documents({"is_archived": False})
    reservations_en_attente = await db.reservations.count_documents({"statut": "En attente"})
    reservations_validees = await db.reservations.count_documents({"statut": "Validée"})
    
    # Stats par branche - support both old (branche) and new (branches) format
    branches_stats = []
    for branche in BRANCHES:
        # Count old format
        count_old = await db.techniciens.count_documents({"is_archived": False, "branche": branche})
        # Count new format (branches contains branche)
        count_new = await db.techniciens.count_documents({"is_archived": False, "branches": branche})
        count = count_old + count_new
        if count > 0:
            branches_stats.append({"branche": branche, "count": count})
    
    return {
        "total_techniciens": total_techniciens,
        "badges_attribues": badges_attribues,
        "badges_non_attribues": total_techniciens - badges_attribues,
        "total_materiel": total_materiel,
        "materiel_disponible": materiel_disponible,
        "devis_en_attente": devis_en_attente,
        "formations_en_attente": formations_en_attente,
        "formations_en_attente_coordination": formations_en_attente_coordination,
        "formations_en_attente_validation_finale": formations_en_attente_validation_finale,
        "branches_stats": branches_stats,
        "total_salles": total_salles,
        "reservations_en_attente": reservations_en_attente,
        "reservations_validees": reservations_validees
    }

# ==================== DASHBOARD MEMBER BRIEF ====================

DEFAULT_SERVICE_INFO_TEXT = "Arrivée 30 minutes avant le début du service — vendredi 18h30, dimanche 8h00."

async def get_service_info_text() -> str:
    """Short reminder of service hours shown on every Dashboard, editable at
    runtime by Admin/Super Admin — same settings-doc pattern as postes."""
    doc = await db.settings.find_one({"_key": "service_info_text"})
    if not doc:
        await db.settings.update_one(
            {"_key": "service_info_text"},
            {"$set": {"_key": "service_info_text", "text": DEFAULT_SERVICE_INFO_TEXT}},
            upsert=True
        )
        return DEFAULT_SERVICE_INFO_TEXT
    return doc.get("text", DEFAULT_SERVICE_INFO_TEXT)

class ServiceInfoUpdate(BaseModel):
    text: str

@api_router.put("/dashboard/service-info")
async def update_service_info_text(data: ServiceInfoUpdate, current_user: dict = Depends(get_current_user)):
    check_access(current_user, ["Super Admin", "Responsable"])
    text = data.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="Le texte ne peut pas être vide")
    await db.settings.update_one({"_key": "service_info_text"}, {"$set": {"text": text}}, upsert=True)
    await log_action(current_user['id'], current_user['full_name'], "Modification rappel horaires", text)
    return {"status": "success", "service_info_text": text}

@api_router.get("/dashboard/member-brief")
async def get_member_brief(current_user: dict = Depends(get_current_user)):
    """Personalized quick-info summary shown at the top of everyone's
    Dashboard (Membre included): next upcoming service date(s) found by
    matching the user's name in this month's/next month's Planning, upcoming
    Actualités events, how many formations are open in the catalogue, and a
    short admin-editable reminder of service hours."""
    now = datetime.now(timezone.utc)
    today_str = now.strftime('%Y-%m-%d')
    target_name = (current_user.get('full_name') or '').strip().lower()

    def _name_matches(value) -> bool:
        if not target_name or not value or not isinstance(value, str):
            return False
        v = value.strip().lower()
        if not v:
            return False
        return v == target_name or v in target_name or target_name in v

    months_to_check = [(now.year, now.month)]
    next_month, next_year = now.month + 1, now.year
    if next_month > 12:
        next_month, next_year = 1, next_year + 1
    months_to_check.append((next_year, next_month))

    upcoming_shifts = []
    for (yr, mo) in months_to_check:
        planning = await db.planning.find_one({"annee": yr, "mois": mo, "is_archived": False}, {"_id": 0})
        if not planning:
            continue
        dates = planning.get('dates') or {}
        affectations = planning.get('affectations') or {}
        for day_type in ['vendredi', 'dimanche']:
            day_dates = dates.get(day_type) or []
            if not day_dates:
                continue
            for values in affectations.values():
                if not values:
                    continue
                items = enumerate(values) if isinstance(values, list) else values.items()
                for idx_key, value in items:
                    try:
                        idx = int(idx_key)
                    except (TypeError, ValueError):
                        continue
                    if idx < 0 or idx >= len(day_dates) or not _name_matches(value):
                        continue
                    date_str = day_dates[idx]
                    if date_str >= today_str:
                        upcoming_shifts.append({"date": date_str, "jour": day_type})

    seen, deduped_shifts = set(), []
    for s in sorted(upcoming_shifts, key=lambda x: x['date']):
        if s['date'] in seen:
            continue
        seen.add(s['date'])
        deduped_shifts.append(s)
    upcoming_shifts = deduped_shifts[:4]

    actualites = await db.actualites.find({"is_active": True}, {"_id": 0}).to_list(200)
    upcoming_events = sorted(
        [a for a in actualites if a.get('date_evenement') and a['date_evenement'] >= today_str],
        key=lambda a: a['date_evenement']
    )[:3]

    formations_catalogue_count = await db.formations.count_documents({"is_archived": False, "disponible_catalogue": True})

    return {
        "upcoming_shifts": upcoming_shifts,
        "upcoming_events": [{"titre": e['titre'], "date_evenement": e['date_evenement']} for e in upcoming_events],
        "formations_catalogue_count": formations_catalogue_count,
        "service_info_text": await get_service_info_text(),
    }

# ==================== ORGANIGRAMME ====================

@api_router.get("/organigramme")
async def get_organigramme(current_user: dict = Depends(get_current_user)):
    org = await db.organigramme.find_one({}, {"_id": 0})
    if not org:
        return {"structure": {"name": "PAV", "children": []}}
    return org

@api_router.put("/organigramme")
async def update_organigramme(data: dict, current_user: dict = Depends(get_current_user)):
    check_access(current_user, ["Super Admin"])
    # Update the structure field
    await db.organigramme.update_one(
        {},
        {"$set": {"structure": data, "updated_at": datetime.now(timezone.utc).isoformat()}},
        upsert=True
    )
    await log_action(current_user['id'], current_user['full_name'], "Modification organigramme", "Organigramme mis à jour")
    return {"status": "success"}

# ==================== LOGS ====================

@api_router.get("/logs", response_model=List[LogResponse])
async def get_logs(current_user: dict = Depends(get_current_user)):
    check_access(current_user, ["Super Admin", "Admin"])
    logs = await db.logs.find({}, {"_id": 0}).sort("timestamp", -1).to_list(500)
    return [LogResponse(**l) for l in logs]

# ==================== SUPERVISION / SYSTEM STATUS ====================
# Answers "how much space is used, is anything close to a crash" without
# needing any external monitoring account — everything here comes from
# MongoDB's own stats commands and the backend process's own clock, so it
# keeps working forever with zero extra credentials to manage.

# Collections we know about — anything not listed here still counts toward
# db_storage_size_bytes/db_data_size_bytes (the whole-database totals), it
# just won't get its own row in the per-collection breakdown.
SUPERVISED_COLLECTIONS = [
    "users", "techniciens", "planning", "absences", "formations",
    "formation_suggestions", "devis", "fournisseurs", "materiel", "salles",
    "creneaux", "reservations", "notifications", "logs", "groups",
    "actualites", "documents", "organigramme", "settings",
]

@api_router.get("/admin/system-status")
async def get_system_status(current_user: dict = Depends(get_current_user)):
    check_access(current_user, ["Super Admin", "Admin"])

    mongo_ok = True
    mongo_error = None
    try:
        await db.command("ping")
    except Exception as e:
        mongo_ok = False
        mongo_error = str(e)

    db_stats = {}
    if mongo_ok:
        try:
            db_stats = await db.command("dbStats")
        except Exception as e:
            mongo_error = str(e)

    collections_info = []
    for name in SUPERVISED_COLLECTIONS:
        try:
            stats = await db.command("collStats", name)
            collections_info.append({
                "name": name,
                "count": stats.get("count", 0),
                "size_bytes": stats.get("size", 0),
            })
        except Exception:
            # Collection doesn't exist yet / empty — not an error, just 0.
            collections_info.append({"name": name, "count": 0, "size_bytes": 0})
    collections_info.sort(key=lambda c: -c["size_bytes"])

    uptime_seconds = time.time() - SERVER_START_TIME

    # Storage quota is admin-configured rather than fetched from an
    # infrastructure API (Railway's MongoDB volumes are billed by usage, not
    # a small fixed quota like a free Atlas cluster, so there's no single
    # "plan limit" number to query). Setting one here is what unlocks a
    # genuine "used / remaining" picture instead of raw bytes alone.
    quota_doc = await db.settings.find_one({"_key": "storage_quota"})
    quota_bytes = (quota_doc or {}).get("quota_bytes")
    storage_size = db_stats.get("storageSize", 0)
    remaining_bytes = (quota_bytes - storage_size) if quota_bytes else None
    percent_used = round((storage_size / quota_bytes) * 100, 1) if quota_bytes else None

    last_logs_purge_doc = await db.settings.find_one({"_key": "last_logs_purge"})

    return {
        "mongo_connected": mongo_ok,
        "mongo_error": mongo_error,
        "db_data_size_bytes": db_stats.get("dataSize", 0),
        "db_storage_size_bytes": storage_size,
        "db_index_size_bytes": db_stats.get("indexSize", 0),
        "db_collections_count": db_stats.get("collections", 0),
        "db_objects_count": db_stats.get("objects", 0),
        "collections": collections_info,
        "backend_uptime_seconds": uptime_seconds,
        "server_time": datetime.now(timezone.utc).isoformat(),
        "quota_bytes": quota_bytes,
        "remaining_bytes": remaining_bytes,
        "percent_used": percent_used,
        "last_logs_purge": (last_logs_purge_doc or {}).get("month"),
    }

def _fetch_json_sync(url: str, headers: dict, timeout: int = 8):
    req = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))

def _request_json_sync(url: str, headers: dict, method: str = "GET", body: dict = None, timeout: int = 15):
    """Same as _fetch_json_sync but supports POST/PUT with a JSON body — used
    for the redeploy/restore actions below (not just read-only status)."""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req_headers = dict(headers)
    if data is not None:
        req_headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, headers=req_headers, method=method, data=data)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8")
        return json.loads(raw) if raw else {}

async def _get_backend_self_check():
    """Live check of this very process + its DB connection — always available,
    no external API key needed."""
    start = time.time()
    mongo_ok = True
    try:
        await db.command("ping")
    except Exception:
        mongo_ok = False
    return {
        "ok": mongo_ok,
        "mongo_connected": mongo_ok,
        "uptime_seconds": time.time() - SERVER_START_TIME,
        "response_time_ms": round((time.time() - start) * 1000, 1),
        "region": "Frankfurt (EU Central)",
        "url": "https://pav-backend-eu.onrender.com",
    }

async def _get_render_status():
    if not RENDER_API_KEY:
        return {
            "configured": False,
            "message": "Ajoutez la variable RENDER_API_KEY (Render > Account Settings > API Keys) pour voir ici le statut, la région, le plan et le dernier déploiement du service en direct.",
        }
    headers = {"Authorization": f"Bearer {RENDER_API_KEY}", "Accept": "application/json"}
    try:
        service = await asyncio.to_thread(
            _fetch_json_sync, f"https://api.render.com/v1/services/{RENDER_SERVICE_ID}", headers
        )
        deploys = await asyncio.to_thread(
            _fetch_json_sync, f"https://api.render.com/v1/services/{RENDER_SERVICE_ID}/deploys?limit=1", headers
        )
        latest = (deploys[0] or {}).get("deploy") if deploys else None
        details = service.get("serviceDetails") or {}
        return {
            "configured": True,
            "ok": not service.get("suspended") or service.get("suspended") == "not_suspended",
            "name": service.get("name"),
            "suspended": service.get("suspended"),
            "region": details.get("region"),
            "plan": details.get("plan"),
            "url": details.get("url"),
            "latest_deploy_status": (latest or {}).get("status"),
            "latest_deploy_at": (latest or {}).get("finishedAt") or (latest or {}).get("createdAt"),
            "latest_deploy_commit": ((latest or {}).get("commit") or {}).get("message"),
        }
    except urllib.error.HTTPError as e:
        return {"configured": True, "ok": False, "error": f"Render API HTTP {e.code} — vérifiez RENDER_API_KEY et RENDER_SERVICE_ID."}
    except Exception as e:
        return {"configured": True, "ok": False, "error": str(e)}

async def _get_netlify_status():
    if not NETLIFY_API_TOKEN or not NETLIFY_SITE_ID:
        return {
            "configured": False,
            "message": "Ajoutez NETLIFY_API_TOKEN (Netlify > User settings > Applications > Personal access tokens) et NETLIFY_SITE_ID (Site settings > General > Site ID) pour voir ici le statut et le dernier déploiement du site en direct.",
        }
    headers = {"Authorization": f"Bearer {NETLIFY_API_TOKEN}", "Accept": "application/json"}
    try:
        site = await asyncio.to_thread(
            _fetch_json_sync, f"https://api.netlify.com/api/v1/sites/{NETLIFY_SITE_ID}", headers
        )
        published = site.get("published_deploy") or {}
        return {
            "configured": True,
            "ok": published.get("state") == "ready",
            "name": site.get("name"),
            "url": site.get("url") or site.get("ssl_url"),
            "state": site.get("state"),
            "latest_deploy_state": published.get("state"),
            "latest_deploy_at": published.get("published_at") or published.get("created_at"),
            "latest_deploy_title": published.get("title") or published.get("commit_ref"),
        }
    except urllib.error.HTTPError as e:
        return {"configured": True, "ok": False, "error": f"Netlify API HTTP {e.code} — vérifiez NETLIFY_API_TOKEN et NETLIFY_SITE_ID."}
    except Exception as e:
        return {"configured": True, "ok": False, "error": str(e)}

@api_router.get("/admin/infra-status")
async def get_infra_status(current_user: dict = Depends(get_current_user)):
    """Consolidated live view of the whole stack for Administration > Supervision:
    this backend + its DB connection (always live), plus the Render (backend
    host) and Netlify (frontend host) services if their API credentials are
    configured — see the RENDER_*/NETLIFY_* env vars above."""
    check_access(current_user, ["Super Admin", "Admin"])
    backend_check, render_info, netlify_info = await asyncio.gather(
        _get_backend_self_check(), _get_render_status(), _get_netlify_status()
    )
    return {
        "backend": backend_check,
        "render": render_info,
        "netlify": netlify_info,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }

# ---- Actions d'infrastructure (Supervision) ----
# Ces routes permettent d'agir directement sur Render/Netlify sans quitter
# l'application (redéployer, consulter l'historique, restaurer un ancien
# déploiement Netlify). Lecture seule à part les deux actions explicitement
# marquées "action" — toutes réservées Super Admin, comme redémarrer le
# serveur ci-dessous, car elles affectent l'infrastructure en direct.

@api_router.get("/admin/infra/render/deploys")
async def list_render_deploys(current_user: dict = Depends(get_current_user)):
    check_access(current_user, ["Super Admin", "Admin"])
    if not RENDER_API_KEY:
        raise HTTPException(status_code=400, detail="RENDER_API_KEY non configurée")
    headers = {"Authorization": f"Bearer {RENDER_API_KEY}", "Accept": "application/json"}
    try:
        deploys = await asyncio.to_thread(
            _fetch_json_sync, f"https://api.render.com/v1/services/{RENDER_SERVICE_ID}/deploys?limit=5", headers
        )
        return [
            {
                "id": (d.get("deploy") or {}).get("id"),
                "status": (d.get("deploy") or {}).get("status"),
                "created_at": (d.get("deploy") or {}).get("createdAt"),
                "finished_at": (d.get("deploy") or {}).get("finishedAt"),
                "commit_message": ((d.get("deploy") or {}).get("commit") or {}).get("message"),
            }
            for d in (deploys or [])
        ]
    except urllib.error.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"Render API HTTP {e.code}")
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))

@api_router.post("/admin/infra/render/redeploy")
async def redeploy_render(current_user: dict = Depends(get_current_user)):
    check_access(current_user, ["Super Admin"])
    if not RENDER_API_KEY:
        raise HTTPException(status_code=400, detail="RENDER_API_KEY non configurée")
    headers = {"Authorization": f"Bearer {RENDER_API_KEY}", "Accept": "application/json"}
    try:
        deploy = await asyncio.to_thread(
            _request_json_sync,
            f"https://api.render.com/v1/services/{RENDER_SERVICE_ID}/deploys",
            headers,
            "POST",
            {"clearCache": "do_not_clear"},
        )
        await log_action(current_user['id'], current_user['full_name'], "Redéploiement backend (Render) déclenché", "")
        return {"message": "Redéploiement lancé — comptez 2 à 5 minutes.", "deploy_id": deploy.get("id")}
    except urllib.error.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"Render API HTTP {e.code}")
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))

@api_router.get("/admin/infra/netlify/deploys")
async def list_netlify_deploys(current_user: dict = Depends(get_current_user)):
    check_access(current_user, ["Super Admin", "Admin"])
    if not NETLIFY_API_TOKEN or not NETLIFY_SITE_ID:
        raise HTTPException(status_code=400, detail="NETLIFY_API_TOKEN / NETLIFY_SITE_ID non configurés")
    headers = {"Authorization": f"Bearer {NETLIFY_API_TOKEN}", "Accept": "application/json"}
    try:
        deploys = await asyncio.to_thread(
            _fetch_json_sync,
            f"https://api.netlify.com/api/v1/sites/{NETLIFY_SITE_ID}/deploys?per_page=5",
            headers,
        )
        return [
            {
                "id": d.get("id"),
                "state": d.get("state"),
                "created_at": d.get("created_at"),
                "title": d.get("title") or d.get("commit_ref"),
                "is_current": d.get("state") == "current" or d.get("published_at") is not None and d.get("state") == "ready",
            }
            for d in (deploys or [])
        ]
    except urllib.error.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"Netlify API HTTP {e.code}")
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))

@api_router.post("/admin/infra/netlify/deploys/{deploy_id}/restore")
async def restore_netlify_deploy(deploy_id: str, current_user: dict = Depends(get_current_user)):
    check_access(current_user, ["Super Admin"])
    if not NETLIFY_API_TOKEN or not NETLIFY_SITE_ID:
        raise HTTPException(status_code=400, detail="NETLIFY_API_TOKEN / NETLIFY_SITE_ID non configurés")
    headers = {"Authorization": f"Bearer {NETLIFY_API_TOKEN}", "Accept": "application/json"}
    try:
        await asyncio.to_thread(
            _request_json_sync,
            f"https://api.netlify.com/api/v1/sites/{NETLIFY_SITE_ID}/deploys/{deploy_id}/restore",
            headers,
            "POST",
            {},
        )
        await log_action(current_user['id'], current_user['full_name'], "Restauration déploiement Netlify", deploy_id)
        return {"message": "Déploiement restauré comme version en ligne."}
    except urllib.error.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"Netlify API HTTP {e.code}")
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))

class StorageQuotaUpdate(BaseModel):
    quota_gb: float  # 0 or omitted clears the quota (back to "no quota set")

@api_router.put("/admin/storage-quota")
async def set_storage_quota(data: StorageQuotaUpdate, current_user: dict = Depends(get_current_user)):
    await check_access_or_role_permission(current_user, ["Super Admin"], "admin.storage_quota")
    quota_bytes = int(data.quota_gb * 1024 * 1024 * 1024) if data.quota_gb and data.quota_gb > 0 else None
    await db.settings.update_one(
        {"_key": "storage_quota"},
        {"$set": {"_key": "storage_quota", "quota_bytes": quota_bytes}},
        upsert=True
    )
    await log_action(current_user['id'], current_user['full_name'], "Modification quota stockage",
                      f"{data.quota_gb} Go" if quota_bytes else "Quota retiré")
    return {"quota_bytes": quota_bytes}

# ---- One-time migration helper: copy every collection from this database ----
# (Railway) into a MongoDB Atlas free-tier cluster. Reads the Atlas connection
# string from the ATLAS_MIGRATION_URL env var (set manually in Railway, never
# committed to git). Super Admin only. Safe to re-run (overwrites destination
# collections each time). Remove this endpoint once the migration is verified.
@api_router.post("/admin/migrate-to-atlas")
async def migrate_to_atlas(current_user: dict = Depends(get_current_user)):
    check_access(current_user, ["Super Admin"])
    atlas_url = os.environ.get("ATLAS_MIGRATION_URL")
    if not atlas_url:
        raise HTTPException(status_code=400, detail="ATLAS_MIGRATION_URL non configurée sur ce service")
    atlas_client = AsyncIOMotorClient(atlas_url, serverSelectionTimeoutMS=15000)
    try:
        await atlas_client.admin.command("ping")
    except Exception as e:
        atlas_client.close()
        raise HTTPException(status_code=502, detail=f"Connexion Atlas échouée: {e}")

    atlas_db = atlas_client[os.environ['DB_NAME']]
    results = {}
    try:
        collection_names = await db.list_collection_names()
        for coll_name in collection_names:
            docs = await db[coll_name].find({}).to_list(length=None)
            if docs:
                await atlas_db[coll_name].delete_many({})
                await atlas_db[coll_name].insert_many(docs)
            dst_count = await atlas_db[coll_name].count_documents({})
            results[coll_name] = {"source": len(docs), "dest": dst_count}
    finally:
        atlas_client.close()

    await log_action(current_user['id'], current_user['full_name'], "Migration base de données",
                      f"{len(results)} collections copiées vers Atlas")
    return {"status": "ok", "collections": results}

# ---- Nettoyage : fichiers orphelins ----
# Un fichier uploadé (collection `files`, servi via GET /api/uploads/{id})
# n'est référencé que depuis deux endroits : actualites.image_url et
# documents.file_url. Un fichier dont l'id n'apparaît dans aucun des deux
# est "orphelin" (remplacé par un autre, ou son actualité/document parent a
# été supprimé) — il n'est plus utilisé nulle part mais continue à occuper
# de la place. Preview (GET) avant suppression (POST) par sécurité : on ne
# supprime jamais sans confirmation explicite.
async def _find_orphaned_file_ids() -> List[str]:
    referenced_ids = set()
    async for a in db.actualites.find({"image_url": {"$exists": True, "$ne": None}}, {"_id": 0, "image_url": 1}):
        url = a.get("image_url") or ""
        if "/api/uploads/" in url:
            referenced_ids.add(url.rsplit("/", 1)[-1])
    async for d in db.documents.find({"file_url": {"$exists": True, "$ne": None}}, {"_id": 0, "file_url": 1}):
        url = d.get("file_url") or ""
        if "/api/uploads/" in url:
            referenced_ids.add(url.rsplit("/", 1)[-1])

    orphaned = []
    async for f in db.files.find({}, {"_id": 0, "id": 1}):
        if f["id"] not in referenced_ids:
            orphaned.append(f["id"])
    return orphaned

@api_router.get("/admin/cleanup/orphaned-files")
async def preview_orphaned_files(current_user: dict = Depends(get_current_user)):
    check_access(current_user, ["Super Admin", "Admin"])
    orphaned_ids = await _find_orphaned_file_ids()
    total_bytes = 0
    if orphaned_ids:
        async for f in db.files.find({"id": {"$in": orphaned_ids}}, {"_id": 0, "size": 1}):
            total_bytes += f.get("size", 0)
    return {"count": len(orphaned_ids), "total_bytes": total_bytes}

@api_router.post("/admin/cleanup/orphaned-files")
async def cleanup_orphaned_files(current_user: dict = Depends(get_current_user)):
    await check_access_or_role_permission(current_user, ["Super Admin"], "admin.cleanup")
    orphaned_ids = await _find_orphaned_file_ids()
    deleted_count = 0
    if orphaned_ids:
        result = await db.files.delete_many({"id": {"$in": orphaned_ids}})
        deleted_count = result.deleted_count
    await log_action(current_user['id'], current_user['full_name'], "Nettoyage fichiers orphelins",
                      f"{deleted_count} fichier(s) supprimé(s)")
    return {"deleted_count": deleted_count}

# ---- Nettoyage : logs d'activité ----
# Purge automatique déjà en place au démarrage (politique RGPD 12 mois) —
# ceci ajoute un déclenchement manuel à la demande, et une vérification en
# tâche de fond qui tourne même si le serveur reste actif plusieurs mois
# sans redémarrer (sinon la purge "au démarrage" ne se représenterait
# jamais). last_logs_purge (settings) retient le mois (YYYY-MM) du dernier
# passage pour ne le faire qu'une fois par mois civil.
async def _purge_old_logs() -> int:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=365)).isoformat()
    result = await db.logs.delete_many({"timestamp": {"$lt": cutoff}})
    current_month = datetime.now(timezone.utc).strftime("%Y-%m")
    await db.settings.update_one(
        {"_key": "last_logs_purge"},
        {"$set": {"_key": "last_logs_purge", "month": current_month}},
        upsert=True
    )
    return result.deleted_count

@api_router.post("/admin/cleanup/logs")
async def cleanup_logs_now(current_user: dict = Depends(get_current_user)):
    await check_access_or_role_permission(current_user, ["Super Admin"], "admin.cleanup")
    deleted_count = await _purge_old_logs()
    await log_action(current_user['id'], current_user['full_name'], "Nettoyage manuel des logs",
                      f"{deleted_count} log(s) de plus de 12 mois supprimé(s)")
    return {"deleted_count": deleted_count}

# ---- Purge totale des logs (immédiate, tout l'historique) ----
# Volontairement plus restreint que le nettoyage +12 mois ci-dessus : réservé
# au titulaire du compte (Guichard) et aux comptes qu'il autorise
# explicitement un par un (log_purge_allowlist dans settings), pas à tous
# les Super Admin — l'historique complet des logs a une valeur d'audit qu'un
# Super Admin ordinaire ne devrait pas pouvoir effacer d'un clic.
async def _get_log_purge_allowlist_ids() -> list:
    doc = await db.settings.find_one({"_key": "log_purge_allowlist"}, {"_id": 0})
    return (doc or {}).get("user_ids", [])

async def _can_purge_all_logs(user: dict) -> bool:
    if is_owner_account(user):
        return True
    allowlist = await _get_log_purge_allowlist_ids()
    return user['id'] in allowlist

@api_router.get("/admin/logs/can-purge-all")
async def get_can_purge_all_logs(current_user: dict = Depends(get_current_user)):
    check_access(current_user, ["Super Admin"])
    return {"allowed": await _can_purge_all_logs(current_user)}

@api_router.get("/admin/logs/purge-allowlist")
async def get_log_purge_allowlist(current_user: dict = Depends(get_current_user)):
    check_access(current_user, ["Super Admin"])
    if not is_owner_account(current_user):
        raise HTTPException(status_code=403, detail="Réservé au titulaire du compte.")
    ids = await _get_log_purge_allowlist_ids()
    users = await db.users.find({"id": {"$in": ids}}, {"_id": 0, "id": 1, "full_name": 1, "username": 1}).to_list(100)
    return users

class LogPurgeAllowlistEntry(BaseModel):
    user_id: str

@api_router.post("/admin/logs/purge-allowlist")
async def add_log_purge_allowlist(data: LogPurgeAllowlistEntry, current_user: dict = Depends(get_current_user)):
    check_access(current_user, ["Super Admin"])
    if not is_owner_account(current_user):
        raise HTTPException(status_code=403, detail="Réservé au titulaire du compte.")
    target = await db.users.find_one({"id": data.user_id}, {"_id": 0})
    if not target:
        raise HTTPException(status_code=404, detail="Utilisateur non trouvé")
    await db.settings.update_one(
        {"_key": "log_purge_allowlist"},
        {"$addToSet": {"user_ids": data.user_id}},
        upsert=True
    )
    await log_action(current_user['id'], current_user['full_name'], "Autorisation purge totale des logs accordée",
                      f"Accordé à : {target.get('full_name')}")
    return {"message": "Autorisation accordée"}

@api_router.delete("/admin/logs/purge-allowlist/{user_id}")
async def remove_log_purge_allowlist(user_id: str, current_user: dict = Depends(get_current_user)):
    check_access(current_user, ["Super Admin"])
    if not is_owner_account(current_user):
        raise HTTPException(status_code=403, detail="Réservé au titulaire du compte.")
    await db.settings.update_one(
        {"_key": "log_purge_allowlist"},
        {"$pull": {"user_ids": user_id}}
    )
    await log_action(current_user['id'], current_user['full_name'], "Autorisation purge totale des logs retirée", user_id)
    return {"message": "Autorisation retirée"}

@api_router.post("/admin/cleanup/logs/purge-all")
async def purge_all_logs_now(current_user: dict = Depends(get_current_user)):
    check_access(current_user, ["Super Admin"])
    if not await _can_purge_all_logs(current_user):
        raise HTTPException(status_code=403, detail="Réservé au titulaire du compte, ou aux comptes qu'il a explicitement autorisés.")
    result = await db.logs.delete_many({})
    deleted_count = result.deleted_count
    await log_action(current_user['id'], current_user['full_name'], "Purge totale des logs",
                      f"{deleted_count} log(s) supprimé(s) — purge immédiate de tout l'historique")
    return {"deleted_count": deleted_count}

async def monthly_logs_purge_task():
    """Runs once a day; actually purges only the first time it notices a
    new calendar month, so it self-corrects even after long uptimes."""
    while True:
        try:
            current_month = datetime.now(timezone.utc).strftime("%Y-%m")
            doc = await db.settings.find_one({"_key": "last_logs_purge"})
            if not doc or doc.get("month") != current_month:
                deleted_count = await _purge_old_logs()
                logger.info(f"Purge mensuelle automatique des logs : {deleted_count} supprimé(s)")
        except Exception as e:
            logger.error(f"Purge mensuelle des logs échouée: {e}")
        await asyncio.sleep(6 * 60 * 60)  # re-check every 6h

# ---- Redémarrage du serveur ----
# Le process se termine lui-même (os._exit) ; Railway relance automatiquement
# tout service dont le process s'arrête de façon inattendue (politique de
# redémarrage par défaut), ce qui a le même effet qu'un vrai "redémarrer" —
# au prix d'une coupure de 30 à 60 secondes le temps que le nouveau
# conteneur démarre. La réponse HTTP est renvoyée avant l'arrêt effectif
# (délai court en tâche de fond) pour que le bouton ne reste pas bloqué.
@api_router.post("/admin/restart-server")
async def restart_server(current_user: dict = Depends(get_current_user)):
    await check_access_or_role_permission(current_user, ["Super Admin"], "admin.restart")
    await log_action(current_user['id'], current_user['full_name'], "Redémarrage serveur demandé", "")

    async def _delayed_exit():
        await asyncio.sleep(1.5)
        os._exit(0)

    asyncio.create_task(_delayed_exit())
    return {"message": "Redémarrage en cours — le serveur sera de nouveau disponible dans environ une minute."}

# ---- Export de données ----
# CSV et JSON (aucune dépendance supplémentaire à installer) sur les
# collections listées dans SUPERVISED_COLLECTIONS uniquement — jamais de
# collection arbitraire par nom, pour éviter d'exposer accidentellement
# quelque chose de sensible qui serait ajouté plus tard sans y penser ici.
@api_router.get("/admin/export/{collection_name}")
async def export_collection(collection_name: str, format: str = "json", current_user: dict = Depends(get_current_user)):
    check_access(current_user, ["Super Admin", "Admin"])
    if collection_name not in SUPERVISED_COLLECTIONS:
        raise HTTPException(status_code=404, detail="Catégorie inconnue")
    if format not in ("json", "csv"):
        raise HTTPException(status_code=400, detail="Format non supporté (json ou csv)")

    docs = await getattr(db, collection_name).find({}, {"_id": 0, "data": 0}).to_list(10000)
    await log_action(current_user['id'], current_user['full_name'], "Export de données",
                      f"{collection_name} ({format}, {len(docs)} élément(s))")

    if format == "json":
        content = json.dumps(docs, indent=2, ensure_ascii=False, default=str)
        return Response(
            content=content,
            media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="{collection_name}.json"'}
        )

    # CSV: union of every key seen across all docs, so no row is truncated
    # just because an earlier doc happened to be missing a field. Nested
    # values (lists/dicts) are serialized as a JSON string within the cell
    # rather than crashing or being silently dropped.
    import csv
    import io
    fieldnames = []
    seen = set()
    for d in docs:
        for k in d.keys():
            if k not in seen:
                seen.add(k)
                fieldnames.append(k)
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    for d in docs:
        row = {}
        for k, v in d.items():
            row[k] = json.dumps(v, ensure_ascii=False, default=str) if isinstance(v, (list, dict)) else v
        writer.writerow(row)
    return Response(
        content=output.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{collection_name}.csv"'}
    )

# ==================== ENUMS ENDPOINTS ====================

async def get_postes_list() -> List[str]:
    """Postes are editable at runtime (add/rename/delete) via /api/postes,
    stored in a single settings doc, seeded from the POSTES constant the
    first time it's read."""
    doc = await db.settings.find_one({"_key": "postes"})
    if not doc:
        await db.settings.update_one(
            {"_key": "postes"},
            {"$set": {"_key": "postes", "list": POSTES}},
            upsert=True
        )
        return list(POSTES)
    return doc.get("list", POSTES)

async def get_competences_formation_list() -> List[str]:
    """Same editable-at-runtime pattern as postes, for the 'compétence
    souhaitée' list used when requesting/proposing a formation."""
    doc = await db.settings.find_one({"_key": "competences_formation"})
    if not doc:
        await db.settings.update_one(
            {"_key": "competences_formation"},
            {"$set": {"_key": "competences_formation", "list": COMPETENCES_FORMATION}},
            upsert=True
        )
        return list(COMPETENCES_FORMATION)
    return doc.get("list", COMPETENCES_FORMATION)

@api_router.get("/enums")
async def get_enums():
    return {
        "niveaux_technicien": NIVEAUX_TECHNICIEN,
        "niveaux_acces": NIVEAUX_ACCES,
        "branches": BRANCHES,
        "sous_branches_live": SOUS_BRANCHES_LIVE,
        "postes": await get_postes_list(),
        "categories_materiel": CATEGORIES_MATERIEL,
        "statuts_devis": STATUTS_DEVIS,
        "statuts_formation": STATUTS_FORMATION,
        "statuts_materiel": STATUTS_MATERIEL,
        "statuts_reservation": STATUTS_RESERVATION,
        "competences_formation": await get_competences_formation_list(),
        "permissions": PERMISSIONS
    }

# ==================== COMPETENCES FORMATION MANAGEMENT (add/rename/delete) ====================

class CompetenceCreate(BaseModel):
    label: str

class CompetenceRename(BaseModel):
    new_label: str

@api_router.post("/competences-formation")
async def add_competence_formation(data: CompetenceCreate, current_user: dict = Depends(get_current_user)):
    check_access(current_user, ["Super Admin"])
    label = data.label.strip()
    if not label:
        raise HTTPException(status_code=400, detail="Le nom de la compétence ne peut pas être vide")
    current = await get_competences_formation_list()
    if label in current:
        raise HTTPException(status_code=400, detail="Cette compétence existe déjà")
    current.append(label)
    await db.settings.update_one({"_key": "competences_formation"}, {"$set": {"list": current}}, upsert=True)
    await log_action(current_user['id'], current_user['full_name'], "Ajout compétence formation", f"Compétence ajoutée: {label}")
    return {"status": "success", "competences_formation": current}

@api_router.put("/competences-formation/{label}")
async def rename_competence_formation(label: str, data: CompetenceRename, current_user: dict = Depends(get_current_user)):
    check_access(current_user, ["Super Admin"])
    new_label = data.new_label.strip()
    if not new_label:
        raise HTTPException(status_code=400, detail="Le nouveau nom ne peut pas être vide")
    current = await get_competences_formation_list()
    if label not in current:
        raise HTTPException(status_code=404, detail="Compétence introuvable")
    if new_label != label and new_label in current:
        raise HTTPException(status_code=400, detail="Cette compétence existe déjà")
    current = [new_label if c == label else c for c in current]
    await db.settings.update_one({"_key": "competences_formation"}, {"$set": {"list": current}}, upsert=True)
    await log_action(current_user['id'], current_user['full_name'], "Renommage compétence formation", f"Compétence renommée: {label} -> {new_label}")
    return {"status": "success", "competences_formation": current}

@api_router.delete("/competences-formation/{label}")
async def delete_competence_formation(label: str, current_user: dict = Depends(get_current_user)):
    check_access(current_user, ["Super Admin"])
    current = await get_competences_formation_list()
    if label not in current:
        raise HTTPException(status_code=404, detail="Compétence introuvable")
    current = [c for c in current if c != label]
    await db.settings.update_one({"_key": "competences_formation"}, {"$set": {"list": current}}, upsert=True)
    await log_action(current_user['id'], current_user['full_name'], "Suppression compétence formation", f"Compétence supprimée: {label}")
    return {"status": "success", "competences_formation": current}

# ==================== POSTES MANAGEMENT (add/rename/delete) ====================

class PosteCreate(BaseModel):
    label: str

class PosteRename(BaseModel):
    new_label: str

@api_router.post("/postes")
async def add_poste(data: PosteCreate, current_user: dict = Depends(get_current_user)):
    check_access(current_user, ["Super Admin"])
    label = data.label.strip()
    if not label:
        raise HTTPException(status_code=400, detail="Le nom du poste ne peut pas être vide")
    current = await get_postes_list()
    if label in current:
        raise HTTPException(status_code=400, detail="Ce poste existe déjà")
    current.append(label)
    await db.settings.update_one({"_key": "postes"}, {"$set": {"list": current}}, upsert=True)
    await log_action(current_user['id'], current_user['full_name'], "Ajout poste", f"Poste ajouté: {label}")
    return {"status": "success", "postes": current}

@api_router.put("/postes/{label}")
async def rename_poste(label: str, data: PosteRename, current_user: dict = Depends(get_current_user)):
    check_access(current_user, ["Super Admin"])
    new_label = data.new_label.strip()
    if not new_label:
        raise HTTPException(status_code=400, detail="Le nouveau nom ne peut pas être vide")
    current = await get_postes_list()
    if label not in current:
        raise HTTPException(status_code=404, detail="Poste introuvable")
    if new_label != label and new_label in current:
        raise HTTPException(status_code=400, detail="Ce poste existe déjà")
    current = [new_label if p == label else p for p in current]
    await db.settings.update_one({"_key": "postes"}, {"$set": {"list": current}}, upsert=True)
    # Propagate the rename to every technicien referencing the old label
    await db.techniciens.update_many({"poste_principal": label}, {"$set": {"poste_principal": new_label}})
    await db.techniciens.update_many(
        {"postes_secondaires": label},
        {"$set": {"postes_secondaires.$[elem]": new_label}},
        array_filters=[{"elem": label}]
    )
    await log_action(current_user['id'], current_user['full_name'], "Renommage poste", f"Poste renommé: {label} -> {new_label}")
    return {"status": "success", "postes": current}

@api_router.delete("/postes/{label}")
async def delete_poste(label: str, current_user: dict = Depends(get_current_user)):
    check_access(current_user, ["Super Admin"])
    current = await get_postes_list()
    if label not in current:
        raise HTTPException(status_code=404, detail="Poste introuvable")
    current = [p for p in current if p != label]
    await db.settings.update_one({"_key": "postes"}, {"$set": {"list": current}}, upsert=True)
    # Clean up references so no technicien fiche points to a deleted poste
    await db.techniciens.update_many({"poste_principal": label}, {"$set": {"poste_principal": None}})
    await db.techniciens.update_many({"postes_secondaires": label}, {"$pull": {"postes_secondaires": label}})
    await log_action(current_user['id'], current_user['full_name'], "Suppression poste", f"Poste supprimé: {label}")
    return {"status": "success", "postes": current}

# ==================== SALLES ROUTES ====================

@api_router.get("/salles", response_model=List[SalleResponse])
async def get_salles(include_archived: bool = False, current_user: dict = Depends(get_current_user)):
    query = {} if include_archived else {"is_archived": False}
    salles = await db.salles.find(query, {"_id": 0}).sort("nom", 1).to_list(100)
    return [SalleResponse(**s) for s in salles]

@api_router.post("/salles", response_model=SalleResponse)
async def create_salle(data: SalleCreate, current_user: dict = Depends(get_current_user)):
    await check_access_or_permission(current_user, ["Super Admin"], "salles.write")
    salle = {
        "id": str(uuid.uuid4()),
        **data.model_dump(),
        "is_archived": False,
        "created_at": datetime.now(timezone.utc).isoformat()
    }
    await db.salles.insert_one(salle)
    await log_action(current_user['id'], current_user['full_name'], "Création salle", f"Salle créée: {data.nom}")
    return SalleResponse(**salle)

@api_router.put("/salles/{salle_id}", response_model=SalleResponse)
async def update_salle(salle_id: str, data: SalleCreate, current_user: dict = Depends(get_current_user)):
    await check_access_or_permission(current_user, ["Super Admin"], "salles.write")
    result = await db.salles.update_one({"id": salle_id}, {"$set": data.model_dump()})
    if result.modified_count == 0:
        raise HTTPException(status_code=404, detail="Salle non trouvée")
    salle = await db.salles.find_one({"id": salle_id}, {"_id": 0})
    await log_action(current_user['id'], current_user['full_name'], "Modification salle", f"Salle modifiée: {data.nom}")
    return SalleResponse(**salle)

@api_router.delete("/salles/{salle_id}")
async def delete_salle(salle_id: str, current_user: dict = Depends(get_current_user)):
    check_access(current_user, ["Super Admin"])
    result = await db.salles.delete_one({"id": salle_id})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Salle non trouvée")
    await log_action(current_user['id'], current_user['full_name'], "Suppression salle", f"Salle supprimée: {salle_id}")
    return {"message": "Salle supprimée"}

# ==================== CRENEAUX ROUTES ====================

@api_router.get("/creneaux", response_model=List[CreneauResponse])
async def get_creneaux(current_user: dict = Depends(get_current_user)):
    creneaux = await db.creneaux.find({"is_active": True}, {"_id": 0}).to_list(50)
    return [CreneauResponse(**c) for c in creneaux]

@api_router.post("/creneaux", response_model=CreneauResponse)
async def create_creneau(data: CreneauCreate, current_user: dict = Depends(get_current_user)):
    await check_access_or_permission(current_user, ["Super Admin"], "salles.write")
    creneau = {
        "id": str(uuid.uuid4()),
        **data.model_dump(),
        "is_active": True,
        "created_at": datetime.now(timezone.utc).isoformat()
    }
    await db.creneaux.insert_one(creneau)
    await log_action(current_user['id'], current_user['full_name'], "Création créneau", f"Créneau créé: {data.nom}")
    return CreneauResponse(**creneau)

@api_router.put("/creneaux/{creneau_id}", response_model=CreneauResponse)
async def update_creneau(creneau_id: str, data: CreneauCreate, current_user: dict = Depends(get_current_user)):
    await check_access_or_permission(current_user, ["Super Admin"], "salles.write")
    result = await db.creneaux.update_one({"id": creneau_id}, {"$set": data.model_dump()})
    if result.modified_count == 0:
        raise HTTPException(status_code=404, detail="Créneau non trouvé")
    creneau = await db.creneaux.find_one({"id": creneau_id}, {"_id": 0})
    await log_action(current_user['id'], current_user['full_name'], "Modification créneau", f"Créneau modifié: {data.nom}")
    return CreneauResponse(**creneau)

@api_router.delete("/creneaux/{creneau_id}")
async def delete_creneau(creneau_id: str, current_user: dict = Depends(get_current_user)):
    check_access(current_user, ["Super Admin"])
    result = await db.creneaux.update_one({"id": creneau_id}, {"$set": {"is_active": False}})
    if result.modified_count == 0:
        raise HTTPException(status_code=404, detail="Créneau non trouvé")
    await log_action(current_user['id'], current_user['full_name'], "Suppression créneau", f"Créneau désactivé: {creneau_id}")
    return {"message": "Créneau supprimé"}

# ==================== SHARE LINKS ROUTES ====================

@api_router.get("/share-links", response_model=List[ShareLinkResponse])
async def get_share_links(current_user: dict = Depends(get_current_user)):
    links = await db.share_links.find({}, {"_id": 0, "mot_de_passe_hash": 0}).sort("created_at", -1).to_list(100)
    return [ShareLinkResponse(**l) for l in links]

@api_router.post("/share-links", response_model=ShareLinkResponse)
async def create_share_link(data: ShareLinkCreate, current_user: dict = Depends(get_current_user)):
    # Everyone can create share links
    token = str(uuid.uuid4())
    expires_at = datetime.now(timezone.utc) + timedelta(hours=data.duree_heures)
    
    link = {
        "id": str(uuid.uuid4()),
        "nom": data.nom,
        "token": token,
        "expires_at": expires_at.isoformat(),
        "has_password": data.mot_de_passe is not None,
        "mot_de_passe_hash": hash_password(data.mot_de_passe) if data.mot_de_passe else None,
        "salles_ids": data.salles_ids,
        "created_by": current_user['id'],
        "created_by_name": current_user['full_name'],
        "created_at": datetime.now(timezone.utc).isoformat(),
        "is_active": True
    }
    await db.share_links.insert_one(link)
    await log_action(current_user['id'], current_user['full_name'], "Création lien partage", f"Lien créé: {data.nom}")
    
    # Return without password hash
    del link['mot_de_passe_hash']
    return ShareLinkResponse(**link)

@api_router.delete("/share-links/{link_id}")
async def delete_share_link(link_id: str, current_user: dict = Depends(get_current_user)):
    result = await db.share_links.update_one({"id": link_id}, {"$set": {"is_active": False}})
    if result.modified_count == 0:
        raise HTTPException(status_code=404, detail="Lien non trouvé")
    await log_action(current_user['id'], current_user['full_name'], "Désactivation lien", f"Lien désactivé: {link_id}")
    return {"message": "Lien désactivé"}

# ==================== PUBLIC SHARE LINK ACCESS ====================

@api_router.post("/public/share/{token}/access")
async def access_share_link(token: str, data: ShareLinkAccess):
    link = await db.share_links.find_one({"token": token, "is_active": True}, {"_id": 0})
    if not link:
        raise HTTPException(status_code=404, detail="Lien invalide ou expiré")
    
    # Check expiration
    expires_at = datetime.fromisoformat(link['expires_at'].replace('Z', '+00:00'))
    if datetime.now(timezone.utc) > expires_at:
        raise HTTPException(status_code=410, detail="Lien expiré")
    
    # Check password if required
    if link['has_password']:
        if not data.mot_de_passe:
            raise HTTPException(status_code=401, detail="Mot de passe requis")
        if not verify_password(data.mot_de_passe, link.get('mot_de_passe_hash', '')):
            raise HTTPException(status_code=401, detail="Mot de passe incorrect")
    
    # Get salles and creneaux
    salles = await db.salles.find({"id": {"$in": link['salles_ids']}, "is_archived": False}, {"_id": 0}).to_list(50)
    creneaux = await db.creneaux.find({"is_active": True}, {"_id": 0}).to_list(50)
    
    return {
        "nom": link['nom'],
        "expires_at": link['expires_at'],
        "salles": salles,
        "creneaux": creneaux
    }

@api_router.get("/public/share/{token}/reservations")
async def get_public_reservations(token: str, date: str):
    link = await db.share_links.find_one({"token": token, "is_active": True}, {"_id": 0})
    if not link:
        raise HTTPException(status_code=404, detail="Lien invalide")
    
    expires_at = datetime.fromisoformat(link['expires_at'].replace('Z', '+00:00'))
    if datetime.now(timezone.utc) > expires_at:
        raise HTTPException(status_code=410, detail="Lien expiré")
    
    # Get reservations for this date and these salles
    reservations = await db.reservations.find({
        "salle_id": {"$in": link['salles_ids']},
        "date": date,
        "statut": {"$in": ["En attente", "Validée"]}
    }, {"_id": 0}).to_list(200)
    
    return reservations

@api_router.post("/public/share/{token}/reservation")
async def create_public_reservation(token: str, data: ReservationCreate):
    link = await db.share_links.find_one({"token": token, "is_active": True}, {"_id": 0})
    if not link:
        raise HTTPException(status_code=404, detail="Lien invalide")
    
    expires_at = datetime.fromisoformat(link['expires_at'].replace('Z', '+00:00'))
    if datetime.now(timezone.utc) > expires_at:
        raise HTTPException(status_code=410, detail="Lien expiré")
    
    # Check salle is allowed
    if data.salle_id not in link['salles_ids']:
        raise HTTPException(status_code=403, detail="Salle non autorisée")
    
    # Check for overlap
    existing = await db.reservations.find_one({
        "salle_id": data.salle_id,
        "date": data.date,
        "creneau_id": data.creneau_id,
        "statut": {"$in": ["En attente", "Validée"]}
    })
    if existing:
        raise HTTPException(status_code=409, detail="Ce créneau est déjà réservé ou en attente de validation")
    
    # Get salle and creneau info
    salle = await db.salles.find_one({"id": data.salle_id}, {"_id": 0})
    creneau = await db.creneaux.find_one({"id": data.creneau_id}, {"_id": 0})
    
    if not salle or not creneau:
        raise HTTPException(status_code=404, detail="Salle ou créneau non trouvé")
    
    reservation = {
        "id": str(uuid.uuid4()),
        "salle_id": data.salle_id,
        "salle_nom": salle['nom'],
        "date": data.date,
        "creneau_id": data.creneau_id,
        "creneau_nom": creneau['nom'],
        "heure_debut": creneau['heure_debut'],
        "heure_fin": creneau['heure_fin'],
        "nom_demandeur": data.nom_demandeur,
        "telephone": data.telephone,
        "email": data.email,
        "raison": data.raison,
        "statut": "En attente",
        "validated_by": None,
        "validated_at": None,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "share_link_id": link['id']
    }
    await db.reservations.insert_one(reservation)

    subject_recue, html_recue = await render_email_template(
        "reservation_recue",
        nom_demandeur=data.nom_demandeur, salle_nom=salle['nom'], date=data.date,
        creneau_nom=creneau['nom'], heure_debut=creneau['heure_debut'], heure_fin=creneau['heure_fin'],
        raison=data.raison,
    )
    fire_and_forget_email(data.email, subject_recue, html_recue)

    subject_valider, html_valider = await render_email_template(
        "reservation_a_valider",
        nom_demandeur=data.nom_demandeur, salle_nom=salle['nom'], date=data.date,
        creneau_nom=creneau['nom'], heure_debut=creneau['heure_debut'], heure_fin=creneau['heure_fin'],
        raison=data.raison,
    )
    for recipient in await resolve_case_recipients("nouvelle_demande"):
        fire_and_forget_email(recipient, subject_valider, html_valider)

    return ReservationResponse(**reservation)

# ==================== RESERVATIONS ROUTES (ADMIN) ====================

@api_router.get("/reservations", response_model=List[ReservationResponse])
async def get_reservations(statut: Optional[str] = None, current_user: dict = Depends(get_current_user)):
    query = {}
    if statut:
        query["statut"] = statut
    reservations = await db.reservations.find(query, {"_id": 0}).sort("created_at", -1).to_list(500)
    return [ReservationResponse(**r) for r in reservations]

@api_router.put("/reservations/{reservation_id}/validate")
async def validate_reservation(reservation_id: str, current_user: dict = Depends(get_current_user)):
    await check_access_or_permission(current_user, ["Super Admin", "Responsable"], "salles.reservations")
    
    # Check for overlap again before validating
    reservation = await db.reservations.find_one({"id": reservation_id}, {"_id": 0})
    if not reservation:
        raise HTTPException(status_code=404, detail="Réservation non trouvée")
    
    existing = await db.reservations.find_one({
        "salle_id": reservation['salle_id'],
        "date": reservation['date'],
        "creneau_id": reservation['creneau_id'],
        "statut": "Validée",
        "id": {"$ne": reservation_id}
    })
    if existing:
        raise HTTPException(status_code=409, detail="Un autre créneau a déjà été validé pour cette salle et horaire")
    
    result = await db.reservations.update_one(
        {"id": reservation_id, "statut": "En attente"},
        {"$set": {
            "statut": "Validée",
            "validated_by": current_user['full_name'],
            "validated_at": datetime.now(timezone.utc).isoformat()
        }}
    )
    if result.modified_count == 0:
        raise HTTPException(status_code=404, detail="Réservation non trouvée ou déjà traitée")
    await log_action(current_user['id'], current_user['full_name'], "Validation réservation", f"Réservation validée: {reservation_id}")
    subject_val, html_val = await render_email_template(
        "reservation_validee",
        nom_demandeur=reservation.get('nom_demandeur', ''), salle_nom=reservation['salle_nom'], date=reservation['date'],
        creneau_nom=reservation['creneau_nom'], heure_debut=reservation['heure_debut'], heure_fin=reservation['heure_fin'],
    )
    fire_and_forget_email(reservation.get('email'), subject_val, html_val)
    for recipient in await resolve_case_recipients("confirmation"):
        fire_and_forget_email(recipient, f"[Confirmation] {subject_val}", html_val)
    return {"message": "Réservation validée"}

@api_router.put("/reservations/{reservation_id}/reject")
async def reject_reservation(reservation_id: str, data: RejectReservationRequest, current_user: dict = Depends(get_current_user)):
    await check_access_or_permission(current_user, ["Super Admin", "Responsable"], "salles.reservations")
    reservation = await db.reservations.find_one({"id": reservation_id}, {"_id": 0})
    if not reservation:
        raise HTTPException(status_code=404, detail="Réservation non trouvée")
    result = await db.reservations.update_one(
        {"id": reservation_id, "statut": "En attente"},
        {"$set": {
            "statut": "Refusée",
            "raison_refus": data.raison_refus,
            "validated_by": current_user['full_name'],
            "validated_at": datetime.now(timezone.utc).isoformat()
        }}
    )
    if result.modified_count == 0:
        raise HTTPException(status_code=404, detail="Réservation non trouvée ou déjà traitée")
    await log_action(current_user['id'], current_user['full_name'], "Refus réservation", f"Réservation refusée: {reservation_id} - Raison: {data.raison_refus}")
    subject_ref, html_ref = await render_email_template(
        "reservation_refusee",
        nom_demandeur=reservation.get('nom_demandeur', ''), salle_nom=reservation['salle_nom'], date=reservation['date'],
        raison_refus=data.raison_refus,
    )
    fire_and_forget_email(reservation.get('email'), subject_ref, html_ref)
    return {"message": "Réservation refusée"}

@api_router.post("/reservations/admin", response_model=ReservationResponse)
async def create_admin_reservation(data: AdminReservationCreate, current_user: dict = Depends(get_current_user)):
    """Admin can directly create and optionally validate a reservation/meeting"""
    await check_access_or_permission(current_user, ["Super Admin", "Responsable"], "salles.reservations")
    
    # Validate salle exists
    salle = await db.salles.find_one({"id": data.salle_id}, {"_id": 0})
    if not salle:
        raise HTTPException(status_code=404, detail="Salle non trouvée")
    
    # Validate creneau exists
    creneau = await db.creneaux.find_one({"id": data.creneau_id}, {"_id": 0})
    if not creneau:
        raise HTTPException(status_code=404, detail="Créneau non trouvé")
    
    # Check for conflicts
    existing = await db.reservations.find_one({
        "salle_id": data.salle_id,
        "date": data.date,
        "creneau_id": data.creneau_id,
        "statut": {"$in": ["En attente", "Validée"]}
    })
    if existing:
        raise HTTPException(status_code=400, detail="Ce créneau est déjà réservé pour cette salle")
    
    now = datetime.now(timezone.utc).isoformat()
    reservation = {
        "id": str(uuid.uuid4()),
        "salle_id": data.salle_id,
        "salle_nom": salle['nom'],
        "date": data.date,
        "creneau_id": data.creneau_id,
        "creneau_nom": creneau['nom'],
        "heure_debut": creneau['heure_debut'],
        "heure_fin": creneau['heure_fin'],
        "nom_demandeur": data.nom_demandeur,
        "telephone": data.telephone or "",
        "email": data.email or "",
        "raison": data.raison,
        "statut": data.statut,
        "raison_refus": None,
        "validated_by": current_user['full_name'] if data.statut == "Validée" else None,
        "validated_at": now if data.statut == "Validée" else None,
        "created_at": now,
        "created_by_admin": True
    }
    
    await db.reservations.insert_one(reservation)
    await log_action(current_user['id'], current_user['full_name'], "Création réunion admin", f"Réunion créée: {data.raison} - {salle['nom']} - {data.date}")
    if data.email:
        title = "Réunion confirmée" if data.statut == "Validée" else "Demande de réservation reçue"
        fire_and_forget_email(data.email, title, email_template(
            title,
            [f"Bonjour {data.nom_demandeur},", f"<b>Salle :</b> {salle['nom']}", f"<b>Date :</b> {data.date}", f"<b>Créneau :</b> {creneau['nom']} ({creneau['heure_debut']}-{creneau['heure_fin']})"],
            kind="success" if data.statut == "Validée" else "pending"
        ))
    return ReservationResponse(**reservation)

@api_router.delete("/reservations/{reservation_id}")
async def delete_reservation(reservation_id: str, current_user: dict = Depends(get_current_user)):
    check_access(current_user, ["Super Admin"])
    result = await db.reservations.delete_one({"id": reservation_id})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Réservation non trouvée")
    await log_action(current_user['id'], current_user['full_name'], "Suppression réservation", f"Réservation supprimée: {reservation_id}")
    return {"message": "Réservation supprimée"}

# ==================== SALLES NOTIFICATIONS SETTINGS ====================
# Backs the "Notifications" tab in Salles: lets an Admin/Responsable edit the
# wording of the 4 reservation emails, and lets an Admin choose who receives
# the two internal-facing ones (per SALLES_NOTIFICATION_CASES above).

@api_router.get("/email-templates")
async def get_email_templates(current_user: dict = Depends(get_current_user)):
    check_access(current_user, ["Super Admin", "Admin", "Responsable"])
    stored = await db.email_templates.find({}, {"_id": 0}).to_list(100)
    stored_map = {t["key"]: t for t in stored}
    result = []
    for key, default in DEFAULT_EMAIL_TEMPLATES.items():
        override = stored_map.get(key, {})
        result.append({
            "key": key,
            "label": default["label"],
            "subject": override.get("subject") or default["subject"],
            "body_lines": override.get("body_lines") or default["body_lines"],
            "kind": default["kind"],
            "placeholders": default["placeholders"],
            "is_customized": key in stored_map,
        })
    return result

class EmailTemplateUpdate(BaseModel):
    subject: str
    body_lines: List[str]

@api_router.put("/email-templates/{key}")
async def update_email_template(key: str, data: EmailTemplateUpdate, current_user: dict = Depends(get_current_user)):
    check_access(current_user, ["Super Admin", "Responsable"])
    if key not in DEFAULT_EMAIL_TEMPLATES:
        raise HTTPException(status_code=404, detail="Modèle inconnu")
    await db.email_templates.update_one(
        {"key": key},
        {"$set": {"key": key, "subject": data.subject, "body_lines": data.body_lines}},
        upsert=True
    )
    await log_action(current_user['id'], current_user['full_name'], "Modification modèle email", f"Modèle modifié: {key}")
    return {"message": "Modèle mis à jour"}

@api_router.post("/email-templates/{key}/reset")
async def reset_email_template(key: str, current_user: dict = Depends(get_current_user)):
    check_access(current_user, ["Super Admin", "Responsable"])
    if key not in DEFAULT_EMAIL_TEMPLATES:
        raise HTTPException(status_code=404, detail="Modèle inconnu")
    await db.email_templates.delete_one({"key": key})
    await log_action(current_user['id'], current_user['full_name'], "Réinitialisation modèle email", f"Modèle réinitialisé: {key}")
    return {"message": "Modèle réinitialisé"}

class NotificationRecipient(BaseModel):
    type: str  # "technicien" | "email"
    id: Optional[str] = None       # technicien id, if type == "technicien"
    value: Optional[str] = None    # raw email, if type == "email"

class NotificationSettingsUpdate(BaseModel):
    recipients: List[NotificationRecipient]

@api_router.get("/salles/notification-settings")
async def get_salles_notification_settings(current_user: dict = Depends(get_current_user)):
    check_access(current_user, ["Super Admin", "Admin", "Responsable"])
    result = {}
    for case, label in SALLES_NOTIFICATION_CASES.items():
        settings = await db.notification_settings.find_one({"case": case}, {"_id": 0})
        recipients = (settings or {}).get("recipients", [])
        resolved = []
        for r in recipients:
            if r.get("type") == "technicien":
                tech = await db.techniciens.find_one({"id": r.get("id")}, {"_id": 0})
                resolved.append({
                    "type": "technicien", "id": r.get("id"),
                    "nom": tech["nom"] if tech else "(technicien supprimé)",
                    "email": tech.get("email") if tech else None,
                })
            else:
                resolved.append({"type": "email", "value": r.get("value")})
        result[case] = {"label": label, "recipients": resolved}
    return result

@api_router.put("/salles/notification-settings/{case}")
async def update_salles_notification_settings(case: str, data: NotificationSettingsUpdate, current_user: dict = Depends(get_current_user)):
    check_access(current_user, ["Super Admin"])
    if case not in SALLES_NOTIFICATION_CASES:
        raise HTTPException(status_code=404, detail="Cas de notification inconnu")
    await db.notification_settings.update_one(
        {"case": case},
        {"$set": {"case": case, "recipients": [r.model_dump() for r in data.recipients]}},
        upsert=True
    )
    await log_action(current_user['id'], current_user['full_name'], "Modification destinataires notification", f"Cas: {case}")
    return {"message": "Destinataires mis à jour"}

# ==================== ENHANCED GROUPS ROUTES ====================

@api_router.get("/groups/enhanced", response_model=List[GroupResponseEnhanced])
async def get_groups_enhanced(current_user: dict = Depends(get_current_user)):
    check_access(current_user, ["Super Admin"])
    groups = await db.groups.find({}, {"_id": 0}).to_list(100)
    result = []
    for g in groups:
        members_count = await db.users.count_documents({"group_ids": g['id']})
        result.append(GroupResponseEnhanced(
            id=g['id'],
            name=g['name'],
            description=g.get('description'),
            permissions=g.get('permissions', []),
            planning_full_control=g.get('planning_full_control', False),
            planning_scope=g.get('planning_scope', []),
            members_count=members_count,
            created_at=g['created_at'],
            updated_at=g.get('updated_at', g['created_at'])
        ))
    return result

@api_router.post("/groups/enhanced", response_model=GroupResponseEnhanced)
async def create_group_enhanced(data: GroupCreateEnhanced, current_user: dict = Depends(get_current_user)):
    check_access(current_user, ["Super Admin"])
    group = {
        "id": str(uuid.uuid4()),
        "name": data.name,
        "description": data.description,
        "permissions": data.permissions,
        "planning_full_control": data.planning_full_control,
        "planning_scope": data.planning_scope,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat()
    }
    await db.groups.insert_one(group)
    await log_action(current_user['id'], current_user['full_name'], "Création groupe", f"Groupe créé: {data.name}")
    return GroupResponseEnhanced(**group, members_count=0)

@api_router.put("/groups/enhanced/{group_id}", response_model=GroupResponseEnhanced)
async def update_group_enhanced(group_id: str, data: GroupCreateEnhanced, current_user: dict = Depends(get_current_user)):
    check_access(current_user, ["Super Admin"])
    update_data = {
        "name": data.name,
        "description": data.description,
        "permissions": data.permissions,
        "planning_full_control": data.planning_full_control,
        "planning_scope": data.planning_scope,
        "updated_at": datetime.now(timezone.utc).isoformat()
    }
    result = await db.groups.update_one({"id": group_id}, {"$set": update_data})
    if result.modified_count == 0:
        raise HTTPException(status_code=404, detail="Groupe non trouvé")
    group = await db.groups.find_one({"id": group_id}, {"_id": 0})
    members_count = await db.users.count_documents({"group_ids": group_id})
    await log_action(current_user['id'], current_user['full_name'], "Modification groupe", f"Groupe modifié: {data.name}")
    return GroupResponseEnhanced(**group, members_count=members_count)

@api_router.delete("/groups/enhanced/{group_id}")
async def delete_group_enhanced(group_id: str, current_user: dict = Depends(get_current_user)):
    check_access(current_user, ["Super Admin"])
    # Remove group from all users
    await db.users.update_many({"group_ids": group_id}, {"$pull": {"group_ids": group_id}})
    result = await db.groups.delete_one({"id": group_id})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Groupe non trouvé")
    await log_action(current_user['id'], current_user['full_name'], "Suppression groupe", f"Groupe supprimé: {group_id}")
    return {"message": "Groupe supprimé"}

@api_router.put("/users/{user_id}/groups")
async def assign_user_groups(user_id: str, data: UserGroupAssignment, current_user: dict = Depends(get_current_user)):
    check_access(current_user, ["Super Admin"])
    result = await db.users.update_one({"id": user_id}, {"$set": {"group_ids": data.group_ids}})
    if result.modified_count == 0:
        raise HTTPException(status_code=404, detail="Utilisateur non trouvé")
    await log_action(current_user['id'], current_user['full_name'], "Attribution groupes", f"Groupes attribués à: {user_id}")
    return {"message": "Groupes attribués"}

class GroupMembersUpdate(BaseModel):
    user_ids: List[str]

# Reverse of assign_user_groups above: manage membership from the group's own
# side (pick which users belong to THIS group) instead of having to open each
# user individually — used by the "Membres" button on each group card in the
# Groupes tab.
@api_router.put("/groups/enhanced/{group_id}/members")
async def update_group_members(group_id: str, data: GroupMembersUpdate, current_user: dict = Depends(get_current_user)):
    check_access(current_user, ["Super Admin"])
    group = await db.groups.find_one({"id": group_id})
    if not group:
        raise HTTPException(status_code=404, detail="Groupe non trouvé")
    await db.users.update_many(
        {"id": {"$in": data.user_ids}, "group_ids": {"$ne": group_id}},
        {"$push": {"group_ids": group_id}}
    )
    await db.users.update_many(
        {"id": {"$nin": data.user_ids}, "group_ids": group_id},
        {"$pull": {"group_ids": group_id}}
    )
    await log_action(current_user['id'], current_user['full_name'], "Modification membres groupe", f"Groupe: {group.get('name')}")
    members_count = await db.users.count_documents({"group_ids": group_id})
    return {"message": "Membres mis à jour", "members_count": members_count}

# ==================== FILE UPLOAD ROUTES ====================

ALLOWED_EXTENSIONS = {'pdf', 'png', 'jpg', 'jpeg', 'gif', 'webp', 'doc', 'docx', 'xls', 'xlsx', 'ppt', 'pptx'}

def allowed_file(filename: str) -> bool:
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

CONTENT_TYPES = {
    'png': 'image/png', 'jpg': 'image/jpeg', 'jpeg': 'image/jpeg', 'gif': 'image/gif', 'webp': 'image/webp',
    'pdf': 'application/pdf',
    'doc': 'application/msword', 'docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    'xls': 'application/vnd.ms-excel', 'xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    'ppt': 'application/vnd.ms-powerpoint', 'pptx': 'application/vnd.openxmlformats-officedocument.presentationml.presentation',
}

@api_router.post("/upload")
async def upload_file(file: UploadFile = File(...), current_user: dict = Depends(get_current_user)):
    """Upload a file (max 10MB). Stored directly in MongoDB (not on local disk):
    Railway's container filesystem is ephemeral and is wiped on every redeploy/
    restart, which used to silently delete previously uploaded images (e.g.
    Actualités photos). MongoDB is the persistent store, so this survives
    redeploys."""
    check_access(current_user, ["Super Admin", "Responsable"])

    if not file.filename:
        raise HTTPException(status_code=400, detail="Aucun fichier fourni")

    if not allowed_file(file.filename):
        raise HTTPException(status_code=400, detail=f"Type de fichier non autorisé. Types acceptés: {', '.join(ALLOWED_EXTENSIONS)}")

    # Check file size
    contents = await file.read()
    if len(contents) > MAX_FILE_SIZE:
        raise HTTPException(status_code=400, detail="Fichier trop volumineux (max 10 MB)")

    ext = file.filename.rsplit('.', 1)[1].lower()
    unique_filename = f"{uuid.uuid4()}.{ext}"

    await db.files.insert_one({
        "id": unique_filename,
        "filename": file.filename,
        "content_type": CONTENT_TYPES.get(ext, "application/octet-stream"),
        "data": contents,
        "size": len(contents),
        "uploaded_by": current_user['id'],
        "created_at": datetime.now(timezone.utc).isoformat()
    })

    file_url = f"/api/uploads/{unique_filename}"

    await log_action(current_user['id'], current_user['full_name'], "Upload fichier", f"Fichier uploadé: {file.filename}")

    return {
        "url": file_url,
        "filename": file.filename,
        "size": len(contents),
        "type": ext
    }

@api_router.get("/uploads/{file_id}")
async def get_uploaded_file(file_id: str):
    """Serve a file previously stored via /upload. Public (like the old static
    mount) since these URLs are embedded directly in pages (e.g. Actualités)."""
    doc = await db.files.find_one({"id": file_id}, {"_id": 0})
    if not doc:
        raise HTTPException(status_code=404, detail="Fichier non trouvé")
    return Response(content=bytes(doc["data"]), media_type=doc.get("content_type", "application/octet-stream"))

# ==================== MAINTENANCE MODE ROUTES ====================

@api_router.get("/maintenance", response_model=MaintenanceModeResponse)
async def get_maintenance_mode():
    """Get maintenance mode status (public endpoint)"""
    maintenance = await db.settings.find_one({"key": "maintenance_mode"}, {"_id": 0})
    if not maintenance:
        return MaintenanceModeResponse(is_active=False, message=None, activated_by=None, activated_at=None, scope="site", page_path=None)
    stored = maintenance.get("value", {})
    stored.setdefault("scope", "site")
    stored.setdefault("page_path", None)
    stored.setdefault("affected_roles", None)
    return MaintenanceModeResponse(**stored)

@api_router.put("/maintenance", response_model=MaintenanceModeResponse)
async def update_maintenance_mode(data: MaintenanceModeUpdate, current_user: dict = Depends(get_current_user)):
    """Toggle maintenance mode (Super Admin, or another role explicitly granted
    via Administration > Droits d'accès)"""
    await check_access_or_role_permission(current_user, ["Super Admin"], "admin.maintenance")

    scope = data.scope if data.scope in ("site", "page") else "site"
    page_path = data.page_path if scope == "page" else None
    if scope == "page" and not page_path:
        raise HTTPException(status_code=400, detail="page_path requis lorsque la portée est 'page'")

    valid_roles = {"Technicien", "Gestionnaire", "Responsable", "Admin"}
    affected_roles = None
    if data.affected_roles:
        affected_roles = [r for r in data.affected_roles if r in valid_roles]
        if not affected_roles:
            raise HTTPException(status_code=400, detail="affected_roles doit contenir au moins un rôle valide")

    now = datetime.now(timezone.utc).isoformat()
    value = {
        "is_active": data.is_active,
        "message": data.message,
        "activated_by": current_user['full_name'] if data.is_active else None,
        "activated_at": now if data.is_active else None,
        "scope": scope,
        "page_path": page_path,
        "affected_roles": affected_roles
    }

    await db.settings.update_one(
        {"key": "maintenance_mode"},
        {"$set": {"key": "maintenance_mode", "value": value, "updated_at": now}},
        upsert=True
    )

    scope_label = "site complet" if scope == "site" else f"page {page_path}"
    action = f"Activation mode maintenance ({scope_label})" if data.is_active else "Désactivation mode maintenance"
    await log_action(current_user['id'], current_user['full_name'], action, data.message or "")

    return MaintenanceModeResponse(**value)

# ==================== STARTUP ====================

@app.on_event("startup")
async def startup():
    await seed_data()
    asyncio.create_task(purge_old_archived_formations())
    asyncio.create_task(monthly_logs_purge_task())
    # Create indexes for better query performance
    try:
        await db.planning.create_index([("annee", 1), ("mois", 1)], unique=True)
        await db.techniciens.create_index("nom")
        await db.reservations.create_index([("salle_id", 1), ("date", 1)])
        # users.id is looked up on nearly every authenticated request (get_current_user);
        # groups.id is looked up on most of those too (permissions/scope resolution).
        # Without these, every request was doing full collection scans on the free
        # Atlas cluster — the main cause of the ~600-700ms per-request lag.
        await db.users.create_index("id", unique=True)
        await db.users.create_index("username", unique=True)
        await db.groups.create_index("id", unique=True)
        logger.info("MongoDB indexes created")
    except Exception as e:
        logger.warning(f"Index creation skipped: {e}")

    # One-off migration: sous_branche (single string) -> sous_branches (list),
    # so existing technicien records keep their sous-branche after the switch
    # to multi-select. Safe to run on every boot (no-op once migrated).
    try:
        async for t in db.techniciens.find({"sous_branche": {"$exists": True}}, {"_id": 0, "id": 1, "sous_branche": 1}):
            val = t.get("sous_branche")
            await db.techniciens.update_one(
                {"id": t["id"]},
                {"$set": {"sous_branches": [val] if val else []}, "$unset": {"sous_branche": ""}}
            )
    except Exception as e:
        logger.warning(f"sous_branche migration skipped: {e}")

    # One-off migration: role label "Membre" was renamed to "Technicien"
    # (same permissions, just a clearer name). Existing users/techniciens
    # records created before the rename still say "Membre" — flip them over.
    # Safe to run on every boot (no-op once migrated).
    try:
        r1 = await db.users.update_many({"niveau_acces": "Membre"}, {"$set": {"niveau_acces": "Technicien"}})
        r2 = await db.techniciens.update_many({"niveau_acces": "Membre"}, {"$set": {"niveau_acces": "Technicien"}})
        if r1.modified_count or r2.modified_count:
            logger.info(f"Migration rôle Membre->Technicien: {r1.modified_count} users, {r2.modified_count} techniciens")
    except Exception as e:
        logger.warning(f"Membre->Technicien migration skipped: {e}")

    # One-off migration: Admin used to unconditionally bypass every
    # check_access_or_permission() call (effectif, planning, devis,
    # formations, logistique, salles, actualités, documents). That bypass
    # was removed so a Super Admin can genuinely revoke individual rights
    # from Admin via Administration > Droits d'accès. To avoid silently
    # breaking Admin's existing access the moment this ships, seed an
    # explicit grant for every "admin_default: True" permission the first
    # time the app boots with this migration — a no-op on every later boot.
    try:
        marker = await db.settings.find_one({"key": "admin_permissions_seeded_v1"})
        if not marker:
            for entry in PERMISSION_CATALOG:
                if entry.get("admin_default"):
                    await db.role_permissions.update_one(
                        {"role": "Admin", "permission": entry["key"]},
                        {"$set": {"role": "Admin", "permission": entry["key"]}},
                        upsert=True,
                    )
            await db.settings.update_one(
                {"key": "admin_permissions_seeded_v1"},
                {"$set": {"key": "admin_permissions_seeded_v1", "value": True}},
                upsert=True,
            )
            logger.info("Droits Admin par défaut initialisés (admin_permissions_seeded_v1)")
    except Exception as e:
        logger.warning(f"Admin permissions seeding skipped: {e}")

    # RGPD: purge activity logs older than 12 months (data retention policy).
    # Shares the same helper as the manual "Nettoyer maintenant" button and
    # the monthly background re-check, so all three stay in sync.
    try:
        deleted_count = await _purge_old_logs()
        if deleted_count:
            logger.info(f"RGPD: {deleted_count} logs de plus de 12 mois purgés")
    except Exception as e:
        logger.warning(f"Log retention purge skipped: {e}")

    logger.info("PAV Management System started")

@app.on_event("shutdown")
async def shutdown_db_client():
    client.close()

app.include_router(api_router)

app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=os.environ.get('CORS_ORIGINS', '*').split(','),
    allow_methods=["*"],
    allow_headers=["*"],
)

# Compress JSON/API responses to cut transfer time, especially on mobile connections.
app.add_middleware(GZipMiddleware, minimum_size=500)
