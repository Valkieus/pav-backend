from fastapi import FastAPI, APIRouter, HTTPException, Depends, status, UploadFile, File
from fastapi.responses import Response
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.staticfiles import StaticFiles
from dotenv import load_dotenv
from starlette.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
import os
import logging
import shutil
import asyncio
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from pathlib import Path
from pydantic import BaseModel, Field
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
NIVEAUX_ACCES = ["Membre", "Gestionnaire", "Responsable", "Admin", "Super Admin"]
BRANCHES = ["Supervision", "Coordination", "Production", "Live", "Animation", "Régisseurs", "Diffusion"]
SOUS_BRANCHES_LIVE = ["Incrustation", "Diffusion", "Cadreur", "Réalisation"]
CATEGORIES_MATERIEL = ["Caméra", "Trépied", "Batterie", "Câble", "Câble HDMI", "Câble SDI", "Câble XLR", "Câble Ethernet", "Micro", "Son", "Lumière", "Moniteur", "Enregistreur", "Accessoire", "Autre"]
STATUTS_DEVIS = ["En attente", "Validé", "Refusé", "Archivé"]
STATUTS_FORMATION = ["En attente Coordination", "En attente validation finale", "Validée", "Refusée", "Archivée"]
STATUTS_MATERIEL = ["Disponible", "En utilisation", "En maintenance", "Hors service", "Archivé"]
STATUTS_RESERVATION = ["En attente", "Validée", "Refusée", "Annulée"]

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
    prenom: str
    niveau_technicien: str
    niveau_acces: str
    branches: List[str]  # Support multiple branches
    sous_branches: Optional[List[str]] = []  # Support multiple sous-branches (Live only)
    badge_attribue: bool = False
    telephone: Optional[str] = None
    email: Optional[str] = None

class TechnicienResponse(BaseModel):
    id: str
    nom: str
    prenom: str
    niveau_technicien: str
    niveau_acces: str
    branches: List[str]  # Support multiple branches
    sous_branches: Optional[List[str]] = []
    badge_attribue: bool
    telephone: Optional[str] = None
    email: Optional[str] = None
    is_archived: bool
    created_at: str
    updated_at: str

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
    date_souhaitee: str
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
    date_souhaitee: str
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

class PlanningCreate(BaseModel):
    mois: int
    annee: int
    dates: dict  # Changed to dict: {dimanche: [], vendredi: []}
    affectations: dict
    sections: Optional[dict] = None
    notes: Optional[dict] = None
    absences: Optional[dict] = None
    blocked_cells: Optional[dict] = None

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

class MaintenanceModeResponse(BaseModel):
    is_active: bool
    message: Optional[str] = None
    activated_by: Optional[str] = None
    activated_at: Optional[str] = None
    scope: Optional[str] = "site"
    page_path: Optional[str] = None

# ==================== GROUP MODELS (ENHANCED) ====================

class GroupCreateEnhanced(BaseModel):
    name: str
    description: Optional[str] = None
    permissions: List[str]

class GroupResponseEnhanced(BaseModel):
    id: str
    name: str
    description: Optional[str] = None
    permissions: List[str]
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

# ==================== EMAIL NOTIFICATIONS ====================
# Free SMTP relay (e.g. a dedicated Gmail account + App Password). Configure via
# Railway environment variables: SMTP_USER, SMTP_PASSWORD, and optionally
# SMTP_HOST/SMTP_PORT/EMAIL_FROM_NAME/ADMIN_NOTIFY_EMAIL. If unset, emails are
# silently skipped so the rest of the app keeps working.
SMTP_HOST = os.environ.get('SMTP_HOST', 'smtp.gmail.com')
SMTP_PORT = int(os.environ.get('SMTP_PORT', '587'))
SMTP_USER = os.environ.get('SMTP_USER')
SMTP_PASSWORD = os.environ.get('SMTP_PASSWORD')
EMAIL_FROM_NAME = os.environ.get('EMAIL_FROM_NAME', 'PAV Manager')
ADMIN_NOTIFY_EMAIL = os.environ.get('ADMIN_NOTIFY_EMAIL') or SMTP_USER
EMAIL_ENABLED = bool(SMTP_USER and SMTP_PASSWORD)

def _send_email_sync(to: str, subject: str, body_html: str):
    msg = MIMEMultipart('alternative')
    msg['Subject'] = subject
    msg['From'] = f"{EMAIL_FROM_NAME} <{SMTP_USER}>"
    msg['To'] = to
    msg.attach(MIMEText(body_html, 'html'))
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=10) as server:
        server.starttls()
        server.login(SMTP_USER, SMTP_PASSWORD)
        server.sendmail(SMTP_USER, [to], msg.as_string())

async def notify_email(to: Optional[str], subject: str, body_html: str):
    """Best-effort transactional email, sent on a worker thread so the blocking
    smtplib call never stalls the event loop. Never raises: a misconfigured or
    down mailbox must not break the underlying workflow action (validation,
    refusal, etc.) — failures are only logged."""
    if not EMAIL_ENABLED or not to:
        return
    try:
        await asyncio.to_thread(_send_email_sync, to, subject, body_html)
    except Exception as e:
        logger.warning(f"Email non envoyé à {to} ({subject}): {e}")

def email_template(title: str, lines: List[str], accent: str = "#B91C1C") -> str:
    rows = "".join(f"<p style='margin:0 0 10px 0;color:#374151;font-size:14px;line-height:1.5'>{l}</p>" for l in lines)
    return f"""
    <div style="font-family:Arial,Helvetica,sans-serif;max-width:520px;margin:0 auto;padding:24px;border:1px solid #e5e7eb;border-radius:8px">
      <div style="border-bottom:3px solid {accent};padding-bottom:12px;margin-bottom:16px">
        <span style="font-size:18px;font-weight:bold;color:#1F2937">PAV Manager</span>
      </div>
      <h2 style="color:#1F2937;font-size:16px;margin:0 0 14px 0">{title}</h2>
      {rows}
      <p style="margin-top:20px;font-size:12px;color:#9CA3AF">Notification automatique — merci de ne pas répondre directement à cet email.</p>
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
            {"nom": "Cynthia B.", "prenom": "", "branches": ["Live"], "sous_branche": None, "niveau_technicien": "Intermédiaire", "niveau_acces": "Membre"},
            {"nom": "Victor", "prenom": "", "branches": ["Live"], "sous_branche": None, "niveau_technicien": "Confirmé", "niveau_acces": "Gestionnaire"},
            {"nom": "Magda", "prenom": "", "branches": ["Live"], "sous_branche": None, "niveau_technicien": "Intermédiaire", "niveau_acces": "Membre"},
            {"nom": "James", "prenom": "", "branches": ["Live"], "sous_branche": None, "niveau_technicien": "Confirmé", "niveau_acces": "Gestionnaire"},
            {"nom": "Prisca", "prenom": "", "branches": ["Live"], "sous_branche": None, "niveau_technicien": "Intermédiaire", "niveau_acces": "Membre"},
            {"nom": "Daniel N", "prenom": "", "branches": ["Live"], "sous_branche": None, "niveau_technicien": "Intermédiaire", "niveau_acces": "Membre"},
            {"nom": "Guichard", "prenom": "", "branches": ["Live", "Supervision"], "sous_branche": None, "niveau_technicien": "Expert", "niveau_acces": "Super Admin"},
            {"nom": "Darwin", "prenom": "", "branches": ["Live"], "sous_branche": None, "niveau_technicien": "Confirmé", "niveau_acces": "Gestionnaire"},
            {"nom": "Elder", "prenom": "", "branches": ["Live"], "sous_branche": None, "niveau_technicien": "Confirmé", "niveau_acces": "Gestionnaire"},
            {"nom": "Yedidjah", "prenom": "", "branches": ["Live"], "sous_branche": None, "niveau_technicien": "Intermédiaire", "niveau_acces": "Membre"},
            {"nom": "Jovani", "prenom": "", "branches": ["Live"], "sous_branche": None, "niveau_technicien": "Intermédiaire", "niveau_acces": "Membre"},
            # Incrustation
            {"nom": "Sybiline", "prenom": "", "branches": ["Live"], "sous_branche": "Incrustation", "niveau_technicien": "Confirmé", "niveau_acces": "Gestionnaire"},
            {"nom": "Joelle", "prenom": "", "branches": ["Live"], "sous_branche": "Incrustation", "niveau_technicien": "Intermédiaire", "niveau_acces": "Membre"},
            {"nom": "Esther", "prenom": "", "branches": ["Live"], "sous_branche": "Incrustation", "niveau_technicien": "Intermédiaire", "niveau_acces": "Membre"},
            {"nom": "Angelo", "prenom": "", "branches": ["Live"], "sous_branche": "Incrustation", "niveau_technicien": "Expert", "niveau_acces": "Responsable"},
            {"nom": "Yuna", "prenom": "", "branches": ["Live"], "sous_branche": "Incrustation", "niveau_technicien": "Débutant", "niveau_acces": "Membre"},
            # VFX
            {"nom": "Fabrice", "prenom": "", "branches": ["Animation"], "sous_branche": None, "niveau_technicien": "Confirmé", "niveau_acces": "Gestionnaire"},
            {"nom": "Laura", "prenom": "", "branches": ["Animation"], "sous_branche": None, "niveau_technicien": "Expert", "niveau_acces": "Responsable"},
            {"nom": "Josué", "prenom": "", "branches": ["Animation"], "sous_branche": None, "niveau_technicien": "Intermédiaire", "niveau_acces": "Membre"},
            {"nom": "Sara", "prenom": "", "branches": ["Animation"], "sous_branche": None, "niveau_technicien": "Intermédiaire", "niveau_acces": "Membre"},
            {"nom": "Nicky", "prenom": "", "branches": ["Animation"], "sous_branche": None, "niveau_technicien": "Confirmé", "niveau_acces": "Gestionnaire"},
            {"nom": "Océane", "prenom": "", "branches": ["Animation"], "sous_branche": None, "niveau_technicien": "Intermédiaire", "niveau_acces": "Membre"},
            # Cadreurs
            {"nom": "Bérénice", "prenom": "", "branches": ["Live"], "sous_branche": "Cadreur", "niveau_technicien": "Confirmé", "niveau_acces": "Gestionnaire"},
            {"nom": "Rebecca", "prenom": "", "branches": ["Live"], "sous_branche": "Cadreur", "niveau_technicien": "Confirmé", "niveau_acces": "Gestionnaire"},
            {"nom": "Martine", "prenom": "", "branches": ["Live"], "sous_branche": "Cadreur", "niveau_technicien": "Intermédiaire", "niveau_acces": "Membre"},
            {"nom": "Ethan", "prenom": "", "branches": ["Live"], "sous_branche": "Cadreur", "niveau_technicien": "Intermédiaire", "niveau_acces": "Membre"},
            {"nom": "Pamela", "prenom": "", "branches": ["Live"], "sous_branche": "Cadreur", "niveau_technicien": "Intermédiaire", "niveau_acces": "Membre"},
            {"nom": "Grace", "prenom": "", "branches": ["Live"], "sous_branche": "Cadreur", "niveau_technicien": "Intermédiaire", "niveau_acces": "Membre"},
            {"nom": "Stacy", "prenom": "", "branches": ["Live"], "sous_branche": "Cadreur", "niveau_technicien": "Intermédiaire", "niveau_acces": "Membre"},
            {"nom": "Marie-Sonie", "prenom": "", "branches": ["Live"], "sous_branche": "Cadreur", "niveau_technicien": "Intermédiaire", "niveau_acces": "Membre"},
            {"nom": "Jean-Wisler", "prenom": "", "branches": ["Live"], "sous_branche": "Cadreur", "niveau_technicien": "Confirmé", "niveau_acces": "Gestionnaire"},
            {"nom": "Jacob", "prenom": "", "branches": ["Live"], "sous_branche": "Cadreur", "niveau_technicien": "Expert", "niveau_acces": "Responsable"},
            {"nom": "Brice", "prenom": "", "branches": ["Live"], "sous_branche": "Cadreur", "niveau_technicien": "Intermédiaire", "niveau_acces": "Membre"},
            {"nom": "Cédric N.", "prenom": "", "branches": ["Live"], "sous_branche": "Cadreur", "niveau_technicien": "Intermédiaire", "niveau_acces": "Membre"},
            {"nom": "Cynthia M.", "prenom": "", "branches": ["Live"], "sous_branche": "Cadreur", "niveau_technicien": "Intermédiaire", "niveau_acces": "Membre"},
            {"nom": "Motler", "prenom": "", "branches": ["Live"], "sous_branche": "Cadreur", "niveau_technicien": "Confirmé", "niveau_acces": "Gestionnaire"},
            {"nom": "Isabelle", "prenom": "", "branches": ["Live"], "sous_branche": "Cadreur", "niveau_technicien": "Intermédiaire", "niveau_acces": "Membre"},
            {"nom": "Junior", "prenom": "", "branches": ["Live"], "sous_branche": "Cadreur", "niveau_technicien": "Intermédiaire", "niveau_acces": "Membre"},
            {"nom": "Brunel", "prenom": "", "branches": ["Live"], "sous_branche": "Cadreur", "niveau_technicien": "Intermédiaire", "niveau_acces": "Membre"},
            {"nom": "Camille", "prenom": "", "branches": ["Live"], "sous_branche": "Cadreur", "niveau_technicien": "Intermédiaire", "niveau_acces": "Membre"},
            {"nom": "Marc-Arthur", "prenom": "", "branches": ["Live"], "sous_branche": "Cadreur", "niveau_technicien": "Confirmé", "niveau_acces": "Gestionnaire"},
            {"nom": "Asony", "prenom": "", "branches": ["Live"], "sous_branche": "Cadreur", "niveau_technicien": "Intermédiaire", "niveau_acces": "Membre"},
            {"nom": "Daniel JP", "prenom": "", "branches": ["Live"], "sous_branche": "Cadreur", "niveau_technicien": "Intermédiaire", "niveau_acces": "Membre"},
            {"nom": "Frandjy", "prenom": "", "branches": ["Live"], "sous_branche": "Cadreur", "niveau_technicien": "Intermédiaire", "niveau_acces": "Membre"},
            # Régisseurs
            {"nom": "Christel", "prenom": "", "branches": ["Production"], "sous_branche": None, "niveau_technicien": "Expert", "niveau_acces": "Responsable"},
            {"nom": "Danarocks", "prenom": "", "branches": ["Production"], "sous_branche": None, "niveau_technicien": "Confirmé", "niveau_acces": "Gestionnaire"},
            {"nom": "Nicolas", "prenom": "", "branches": ["Production", "Logistique"], "sous_branche": None, "niveau_technicien": "Confirmé", "niveau_acces": "Gestionnaire"},
            {"nom": "Elvis", "prenom": "", "branches": ["Production"], "sous_branche": None, "niveau_technicien": "Intermédiaire", "niveau_acces": "Membre"},
            {"nom": "Emmanuella", "prenom": "", "branches": ["Production"], "sous_branche": None, "niveau_technicien": "Intermédiaire", "niveau_acces": "Membre"},
            {"nom": "Eloise", "prenom": "", "branches": ["Production"], "sous_branche": None, "niveau_technicien": "Intermédiaire", "niveau_acces": "Membre"},
            {"nom": "Edese", "prenom": "", "branches": ["Production"], "sous_branche": None, "niveau_technicien": "Intermédiaire", "niveau_acces": "Membre"},
            {"nom": "Sherley", "prenom": "", "branches": ["Production"], "sous_branche": None, "niveau_technicien": "Intermédiaire", "niveau_acces": "Membre"},
            {"nom": "Judite", "prenom": "", "branches": ["Production"], "sous_branche": None, "niveau_technicien": "Intermédiaire", "niveau_acces": "Membre"},
            {"nom": "Joanna", "prenom": "", "branches": ["Logistique"], "sous_branche": None, "niveau_technicien": "Expert", "niveau_acces": "Responsable"},
            # Diffusion - Renaud is Responsable de la Diffusion
            {"nom": "Renaud", "prenom": "", "branches": ["Live"], "sous_branche": "Diffusion", "niveau_technicien": "Expert", "niveau_acces": "Responsable"},
            {"nom": "Harvey", "prenom": "", "branches": ["Live"], "sous_branche": "Diffusion", "niveau_technicien": "Confirmé", "niveau_acces": "Gestionnaire"},
            {"nom": "Jean-Remy Victor", "prenom": "", "branches": ["Live"], "sous_branche": "Diffusion", "niveau_technicien": "Confirmé", "niveau_acces": "Gestionnaire"},
            {"nom": "Cedric", "prenom": "", "branches": ["Live"], "sous_branche": "Diffusion", "niveau_technicien": "Intermédiaire", "niveau_acces": "Membre"},
            {"nom": "Dierry", "prenom": "", "branches": ["Live"], "sous_branche": "Diffusion", "niveau_technicien": "Intermédiaire", "niveau_acces": "Membre"},
            {"nom": "Tresor", "prenom": "", "branches": ["Live"], "sous_branche": "Diffusion", "niveau_technicien": "Intermédiaire", "niveau_acces": "Membre"},
            {"nom": "Michael", "prenom": "", "branches": ["Live"], "sous_branche": "Diffusion", "niveau_technicien": "Intermédiaire", "niveau_acces": "Membre"},
            {"nom": "Yves", "prenom": "", "branches": ["Live"], "sous_branche": "Diffusion", "niveau_technicien": "Intermédiaire", "niveau_acces": "Membre"},
            {"nom": "Esdras", "prenom": "", "branches": ["Live"], "sous_branche": "Diffusion", "niveau_technicien": "Intermédiaire", "niveau_acces": "Membre"},
            {"nom": "Joseph", "prenom": "", "branches": ["Live"], "sous_branche": "Diffusion", "niveau_technicien": "Intermédiaire", "niveau_acces": "Membre"},
            # Coordination
            {"nom": "Delphine", "prenom": "", "branches": ["Coordination"], "sous_branche": None, "niveau_technicien": "Expert", "niveau_acces": "Responsable"},
            {"nom": "Winchel", "prenom": "", "branches": ["Coordination"], "sous_branche": None, "niveau_technicien": "Expert", "niveau_acces": "Responsable"},
            {"nom": "Paul Baptista", "prenom": "", "branches": ["Supervision"], "sous_branche": None, "niveau_technicien": "Expert", "niveau_acces": "Admin"},
            {"nom": "Ryan", "prenom": "", "branches": ["Supervision"], "sous_branche": None, "niveau_technicien": "Expert", "niveau_acces": "Admin"},
            # Additional for FCP/Intercom
            {"nom": "Paul", "prenom": "", "branches": ["Live"], "sous_branche": None, "niveau_technicien": "Confirmé", "niveau_acces": "Gestionnaire"},
            {"nom": "Tchaba", "prenom": "", "branches": ["Live"], "sous_branche": None, "niveau_technicien": "Intermédiaire", "niveau_acces": "Membre"},
            {"nom": "Coralie", "prenom": "", "branches": ["Live"], "sous_branche": None, "niveau_technicien": "Intermédiaire", "niveau_acces": "Membre"},
            {"nom": "Balikissou", "prenom": "", "branches": ["Live"], "sous_branche": None, "niveau_technicien": "Intermédiaire", "niveau_acces": "Membre"},
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
            is_active=user.get('is_active', True)
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
        must_change_password=current_user.get('must_change_password', False)
    )

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
    await notify_email(ADMIN_NOTIFY_EMAIL, "Demande RGPD — suppression de compte", email_template(
        "Un utilisateur demande la suppression de son compte",
        [f"<b>Utilisateur :</b> {current_user['full_name']} ({current_user['username']})",
         "À traiter sous 30 jours conformément au RGPD, depuis Administration → Utilisateurs."]
    ))
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
        "must_change_password": True  # Force password change on first login
    }
    await db.users.insert_one(user)
    await log_action(current_user['id'], current_user['full_name'], "Création utilisateur", f"Utilisateur créé: {data.username}")
    
    return UserResponse(
        id=user['id'], username=user['username'], full_name=user['full_name'],
        niveau_acces=user['niveau_acces'], branches=user['branches'], created_at=user['created_at'], is_active=user['is_active'],
        must_change_password=user['must_change_password']
    )

@api_router.put("/auth/users/{user_id}")
async def update_user(user_id: str, data: UserUpdate, current_user: dict = Depends(get_current_user)):
    check_access(current_user, ["Super Admin"])
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
    users = await db.users.find({}, {"_id": 0, "password": 0}).to_list(1000)
    return [UserResponse(**u) for u in users]

@api_router.delete("/auth/users/{user_id}")
async def delete_user(user_id: str, current_user: dict = Depends(get_current_user)):
    check_access(current_user, ["Super Admin"])
    if current_user['id'] == user_id:
        raise HTTPException(status_code=400, detail="Impossible de supprimer votre propre compte")
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
        "niveau_acces": "Membre",
        "branches": technicien.get("branches", []),
        "technicien_id": technicien["id"],
        "created_at": datetime.now(timezone.utc).isoformat(),
        "is_active": True,
        "must_change_password": False
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
            created_at=user['created_at'], is_active=user['is_active'], must_change_password=user['must_change_password']
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
    techniciens = await db.techniciens.find(query, {"_id": 0}).sort("nom", 1).to_list(1000)
    return [TechnicienResponse(**normalize_technicien(t)) for t in techniciens]

@api_router.post("/techniciens", response_model=TechnicienResponse)
async def create_technicien(data: TechnicienCreate, current_user: dict = Depends(get_current_user)):
    check_access(current_user, ["Super Admin", "Admin", "Responsable"])
    tech = {
        "id": str(uuid.uuid4()),
        **data.model_dump(),
        "is_archived": False,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat()
    }
    await db.techniciens.insert_one(tech)
    await log_action(current_user['id'], current_user['full_name'], "Création technicien", f"Technicien créé: {data.nom} {data.prenom}")
    return TechnicienResponse(**tech)

@api_router.put("/techniciens/{tech_id}", response_model=TechnicienResponse)
async def update_technicien(tech_id: str, data: TechnicienCreate, current_user: dict = Depends(get_current_user)):
    check_access(current_user, ["Super Admin", "Admin", "Responsable"])
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
    check_access(current_user, ["Super Admin", "Admin"])
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

# ==================== MATERIEL ROUTES ====================

@api_router.get("/materiel", response_model=List[MaterielResponse])
async def get_materiel(include_archived: bool = False, current_user: dict = Depends(get_current_user)):
    query = {} if include_archived else {"is_archived": False}
    materiels = await db.materiel.find(query, {"_id": 0}).to_list(1000)
    return [MaterielResponse(**m) for m in materiels]

@api_router.post("/materiel", response_model=MaterielResponse)
async def create_materiel(data: MaterielCreate, current_user: dict = Depends(get_current_user)):
    check_access(current_user, ["Super Admin", "Admin", "Responsable", "Gestionnaire"])
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
    check_access(current_user, ["Super Admin", "Admin", "Responsable", "Gestionnaire"])
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
    check_access(current_user, ["Super Admin", "Admin"])
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
    check_access(current_user, ["Super Admin", "Admin"])
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
    check_access(current_user, ["Super Admin", "Admin", "Responsable"])
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
    check_access(current_user, ["Super Admin", "Admin", "Responsable"])
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
    check_access(current_user, ["Super Admin", "Admin"])
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
    await notify_email(ADMIN_NOTIFY_EMAIL, "Nouveau devis en attente de validation", email_template(
        "Un nouveau devis attend une validation",
        [f"<b>Titre :</b> {data.titre}", f"<b>Montant :</b> {data.montant} €", f"<b>Demandé par :</b> {current_user['full_name']}"]
    ))
    return DevisResponse(**devis)

@api_router.put("/devis/{devis_id}/validate")
async def validate_devis(devis_id: str, current_user: dict = Depends(get_current_user)):
    check_access(current_user, ["Super Admin", "Admin", "Responsable"])
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
    await notify_email(await get_user_email(devis['created_by']), "Devis validé", email_template(
        "Votre devis a été validé", [f"<b>Titre :</b> {devis['titre']}", f"<b>Montant :</b> {devis['montant']} €"], accent="#059669"
    ))
    return {"message": "Devis validé"}

@api_router.put("/devis/{devis_id}/reject")
async def reject_devis(devis_id: str, current_user: dict = Depends(get_current_user)):
    check_access(current_user, ["Super Admin", "Admin", "Responsable"])
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
    await notify_email(await get_user_email(devis['created_by']), "Devis refusé", email_template(
        "Votre devis a été refusé", [f"<b>Titre :</b> {devis['titre']}", f"<b>Montant :</b> {devis['montant']} €"]
    ))
    return {"message": "Devis refusé"}

@api_router.put("/devis/{devis_id}/revert")
async def revert_devis(devis_id: str, current_user: dict = Depends(get_current_user)):
    """Send a Validé/Refusé devis back to 'En attente' so it can be re-reviewed."""
    check_access(current_user, ["Super Admin", "Admin", "Responsable"])
    devis = await db.devis.find_one({"id": devis_id}, {"_id": 0})
    result = await db.devis.update_one(
        {"id": devis_id, "statut": {"$in": ["Validé", "Refusé"]}},
        {"$set": {"statut": "En attente", "validated_by": None, "validated_at": None}}
    )
    if result.modified_count == 0:
        raise HTTPException(status_code=404, detail="Devis non trouvé ou déjà en attente")
    await log_action(current_user['id'], current_user['full_name'], "Retour devis en attente", f"Devis remis en attente: {devis_id}")
    if devis:
        await notify_email(await get_user_email(devis['created_by']), "Devis remis en attente", email_template(
            "Votre devis a été remis en attente de validation", [f"<b>Titre :</b> {devis['titre']}"]
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
    check_access(current_user, ["Super Admin", "Admin"])
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

@api_router.get("/formations", response_model=List[FormationResponse])
async def get_formations(include_archived: bool = False, current_user: dict = Depends(get_current_user)):
    query = {} if include_archived else {"is_archived": False}
    formations = await db.formations.find(query, {"_id": 0}).to_list(1000)
    return [FormationResponse(**f) for f in formations]

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
        "is_archived": False
    }
    await db.formations.insert_one(formation)
    await log_action(current_user['id'], current_user['full_name'], "Demande formation", f"Formation demandée: {data.titre}")
    await notify_email(ADMIN_NOTIFY_EMAIL, "Nouvelle demande de formation", email_template(
        "Une nouvelle demande de formation attend la Coordination",
        [f"<b>Titre :</b> {data.titre}", f"<b>Demandé par :</b> {current_user['full_name']}", f"<b>Date souhaitée :</b> {data.date_souhaitee}"]
    ))
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
        "date_souhaitee": data.date_souhaitee,
        "duree": data.duree,
        "updated_at": datetime.now(timezone.utc).isoformat()
    }
    await db.formations.update_one({"id": formation_id}, {"$set": update_data})
    await log_action(current_user['id'], current_user['full_name'], "Modification formation", f"Formation modifiée: {data.titre}")
    formation = await db.formations.find_one({"id": formation_id}, {"_id": 0})
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
    await notify_email(await get_user_email(formation['created_by']), "Votre demande de formation avance", email_template(
        "Votre demande de formation est transmise pour validation finale",
        [f"<b>Titre :</b> {formation['titre']}", f"<b>Formateur :</b> {formation.get('formateur') or '-'}", f"<b>Lieu :</b> {formation.get('lieu') or '-'}"]
    ))
    await notify_email(ADMIN_NOTIFY_EMAIL, "Formation en attente de validation finale", email_template(
        "Une formation attend la validation finale de la Direction", [f"<b>Titre :</b> {formation['titre']}"]
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
    await notify_email(await get_user_email(formation['created_by']), "Demande de formation refusée", email_template(
        "Votre demande de formation a été refusée", [f"<b>Titre :</b> {formation['titre']}", f"<b>Motif :</b> {data.motif or '-'}"]
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
    await notify_email(await get_user_email(formation['created_by']), "Formation validée", email_template(
        "Votre demande de formation a été validée", [f"<b>Titre :</b> {formation['titre']}", f"<b>Date souhaitée :</b> {formation.get('date_souhaitee') or '-'}"], accent="#059669"
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
    await notify_email(await get_user_email(formation['created_by']), "Demande de formation refusée", email_template(
        "Votre demande de formation a été refusée en validation finale", [f"<b>Titre :</b> {formation['titre']}", f"<b>Motif :</b> {data.motif or '-'}"]
    ))
    return FormationResponse(**formation)

@api_router.put("/formations/{formation_id}/archive")
async def archive_formation(formation_id: str, current_user: dict = Depends(get_current_user)):
    check_access(current_user, ["Super Admin", "Admin"])
    result = await db.formations.update_one({"id": formation_id}, {"$set": {"is_archived": True}})
    if result.modified_count == 0:
        raise HTTPException(status_code=404, detail="Formation non trouvée")
    await log_action(current_user['id'], current_user['full_name'], "Archivage formation", f"Formation archivée: {formation_id}")
    return {"message": "Formation archivée"}

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

@api_router.post("/planning", response_model=PlanningResponse)
async def create_planning(data: PlanningCreate, current_user: dict = Depends(get_current_user)):
    # Gestionnaire is included so they can save the Absences/Notes fields they're
    # allowed to edit in the web Planning; the UI itself keeps the affectation
    # grid cells read-only for anyone below Responsable.
    check_access(current_user, ["Super Admin", "Admin", "Responsable", "Gestionnaire"])
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
    check_access(current_user, ["Super Admin", "Admin", "Responsable", "Gestionnaire"])
    update_data = {
        "dates": data.dates,
        "affectations": data.affectations,
        "sections": data.sections,
        "notes": data.notes,
        "absences": data.absences,
        "blocked_cells": data.blocked_cells,
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
    check_access(current_user, ["Super Admin", "Admin"])
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
    check_access(current_user, ["Super Admin", "Admin", "Responsable", "Gestionnaire"])
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
    check_access(current_user, ["Super Admin", "Admin", "Responsable", "Gestionnaire"])
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
    check_access(current_user, ["Super Admin", "Admin"])
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
    check_access(current_user, ["Super Admin", "Admin"])
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
    check_access(current_user, ["Super Admin", "Admin"])
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
    for doc in documents:
        doc['categorie_nom'] = categories.get(doc.get('categorie_id'), 'Sans catégorie')
    return [DocumentResponse(**d) for d in documents]

@api_router.post("/documents", response_model=DocumentResponse)
async def create_document(data: DocumentCreate, current_user: dict = Depends(get_current_user)):
    check_access(current_user, ["Super Admin", "Admin", "Responsable", "Gestionnaire"])
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
        "created_by": current_user['id'],
        "created_by_name": current_user['full_name'],
        "created_at": datetime.now(timezone.utc).isoformat()
    }
    await db.documents.insert_one(document)
    await log_action(current_user['id'], current_user['full_name'], "Ajout document", f"Document ajouté: {data.titre}")
    return DocumentResponse(**document)

@api_router.put("/documents/{document_id}", response_model=DocumentResponse)
async def update_document(document_id: str, data: DocumentCreate, current_user: dict = Depends(get_current_user)):
    check_access(current_user, ["Super Admin", "Admin", "Responsable", "Gestionnaire"])
    category = await db.document_categories.find_one({"id": data.categorie_id}, {"_id": 0})
    categorie_nom = category['nom'] if category else 'Sans catégorie'
    
    update_data = {
        "titre": data.titre,
        "categorie_id": data.categorie_id,
        "categorie_nom": categorie_nom,
        "description": data.description,
        "file_url": data.file_url,
        "file_type": data.file_type
    }
    result = await db.documents.update_one({"id": document_id}, {"$set": update_data})
    if result.modified_count == 0:
        raise HTTPException(status_code=404, detail="Document non trouvé")
    document = await db.documents.find_one({"id": document_id}, {"_id": 0})
    return DocumentResponse(**document)

@api_router.delete("/documents/{document_id}")
async def delete_document(document_id: str, current_user: dict = Depends(get_current_user)):
    check_access(current_user, ["Super Admin", "Admin"])
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

# ==================== ORGANIGRAMME ====================

@api_router.get("/organigramme")
async def get_organigramme(current_user: dict = Depends(get_current_user)):
    org = await db.organigramme.find_one({}, {"_id": 0})
    if not org:
        return {"structure": {"name": "PAV", "children": []}}
    return org

@api_router.put("/organigramme")
async def update_organigramme(data: dict, current_user: dict = Depends(get_current_user)):
    check_access(current_user, ["Super Admin", "Admin"])
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

# ==================== ENUMS ENDPOINTS ====================

@api_router.get("/enums")
async def get_enums():
    return {
        "niveaux_technicien": NIVEAUX_TECHNICIEN,
        "niveaux_acces": NIVEAUX_ACCES,
        "branches": BRANCHES,
        "sous_branches_live": SOUS_BRANCHES_LIVE,
        "categories_materiel": CATEGORIES_MATERIEL,
        "statuts_devis": STATUTS_DEVIS,
        "statuts_formation": STATUTS_FORMATION,
        "statuts_materiel": STATUTS_MATERIEL,
        "statuts_reservation": STATUTS_RESERVATION,
        "permissions": PERMISSIONS
    }

# ==================== SALLES ROUTES ====================

@api_router.get("/salles", response_model=List[SalleResponse])
async def get_salles(include_archived: bool = False, current_user: dict = Depends(get_current_user)):
    query = {} if include_archived else {"is_archived": False}
    salles = await db.salles.find(query, {"_id": 0}).sort("nom", 1).to_list(100)
    return [SalleResponse(**s) for s in salles]

@api_router.post("/salles", response_model=SalleResponse)
async def create_salle(data: SalleCreate, current_user: dict = Depends(get_current_user)):
    check_access(current_user, ["Super Admin", "Admin"])
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
    check_access(current_user, ["Super Admin", "Admin"])
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
    check_access(current_user, ["Super Admin", "Admin"])
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
    check_access(current_user, ["Super Admin", "Admin"])
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

    await notify_email(data.email, "Demande de réservation reçue", email_template(
        "Votre demande de réservation a bien été reçue",
        [f"<b>Salle :</b> {salle['nom']}", f"<b>Date :</b> {data.date}",
         f"<b>Créneau :</b> {creneau['nom']} ({creneau['heure_debut']}-{creneau['heure_fin']})",
         "Elle est en attente de validation — vous recevrez un email dès qu'elle sera traitée."]
    ))
    await notify_email(ADMIN_NOTIFY_EMAIL, "Nouvelle demande de réservation à valider", email_template(
        "Une nouvelle demande de réservation attend une validation",
        [f"<b>Demandeur :</b> {data.nom_demandeur}", f"<b>Salle :</b> {salle['nom']}", f"<b>Date :</b> {data.date}",
         f"<b>Créneau :</b> {creneau['nom']}", f"<b>Raison :</b> {data.raison}"]
    ))

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
    check_access(current_user, ["Super Admin", "Admin", "Responsable"])
    
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
    await notify_email(reservation.get('email'), "Réservation validée", email_template(
        "Votre réservation a été validée",
        [f"<b>Salle :</b> {reservation['salle_nom']}", f"<b>Date :</b> {reservation['date']}",
         f"<b>Créneau :</b> {reservation['creneau_nom']} ({reservation['heure_debut']}-{reservation['heure_fin']})"],
        accent="#059669"
    ))
    return {"message": "Réservation validée"}

@api_router.put("/reservations/{reservation_id}/reject")
async def reject_reservation(reservation_id: str, data: RejectReservationRequest, current_user: dict = Depends(get_current_user)):
    check_access(current_user, ["Super Admin", "Admin", "Responsable"])
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
    await notify_email(reservation.get('email'), "Réservation refusée", email_template(
        "Votre réservation n'a pas été retenue",
        [f"<b>Salle :</b> {reservation['salle_nom']}", f"<b>Date :</b> {reservation['date']}",
         f"<b>Raison du refus :</b> {data.raison_refus}"]
    ))
    return {"message": "Réservation refusée"}

@api_router.post("/reservations/admin", response_model=ReservationResponse)
async def create_admin_reservation(data: AdminReservationCreate, current_user: dict = Depends(get_current_user)):
    """Admin can directly create and optionally validate a reservation/meeting"""
    check_access(current_user, ["Super Admin", "Admin", "Responsable"])
    
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
        await notify_email(data.email, title, email_template(
            title, [f"<b>Salle :</b> {salle['nom']}", f"<b>Date :</b> {data.date}", f"<b>Créneau :</b> {creneau['nom']} ({creneau['heure_debut']}-{creneau['heure_fin']})"],
            accent="#059669" if data.statut == "Validée" else "#B91C1C"
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
    check_access(current_user, ["Super Admin", "Admin", "Responsable"])

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
    return MaintenanceModeResponse(**stored)

@api_router.put("/maintenance", response_model=MaintenanceModeResponse)
async def update_maintenance_mode(data: MaintenanceModeUpdate, current_user: dict = Depends(get_current_user)):
    """Toggle maintenance mode (Super Admin only)"""
    check_access(current_user, ["Super Admin"])

    scope = data.scope if data.scope in ("site", "page") else "site"
    page_path = data.page_path if scope == "page" else None
    if scope == "page" and not page_path:
        raise HTTPException(status_code=400, detail="page_path requis lorsque la portée est 'page'")

    now = datetime.now(timezone.utc).isoformat()
    value = {
        "is_active": data.is_active,
        "message": data.message,
        "activated_by": current_user['full_name'] if data.is_active else None,
        "activated_at": now if data.is_active else None,
        "scope": scope,
        "page_path": page_path
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
    # Create indexes for better query performance
    try:
        await db.planning.create_index([("annee", 1), ("mois", 1)], unique=True)
        await db.techniciens.create_index("nom")
        await db.reservations.create_index([("salle_id", 1), ("date", 1)])
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

    # RGPD: purge activity logs older than 12 months (data retention policy)
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=365)).isoformat()
        result = await db.logs.delete_many({"timestamp": {"$lt": cutoff}})
        if result.deleted_count:
            logger.info(f"RGPD: {result.deleted_count} logs de plus de 12 mois purgés")
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
