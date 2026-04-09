from datetime import datetime, timedelta
from typing import List, Dict, Optional
import re
from collections import defaultdict
import os
import hmac
import hashlib

from fastapi import FastAPI, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, EmailStr
from jose import JWTError, jwt

from sqlalchemy import (
    create_engine, Column, Integer, String, Float,
    ForeignKey, UniqueConstraint, func, Boolean, DateTime, Text
)
from sqlalchemy.orm import sessionmaker, declarative_base, Session, relationship

from google.oauth2 import id_token
from google.auth.transport import requests as grequests
import razorpay

# ======================
# CONFIG
# ======================

SECRET_KEY = "supersecretkey_change_this"  # <-- change in production
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24  # 1 day

# 🔑 Replace with your Google OAuth client ID
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "938605090290-spglalmtou8cnn22j6j7he1q82foeho8.apps.googleusercontent.com")

# 💳 Razorpay keys (replace with your keys)
RAZORPAY_KEY_ID = os.getenv("RAZORPAY_KEY_ID", "rzp_live_RyAIo1CW9uPG0R")
RAZORPAY_KEY_SECRET = os.getenv("RAZORPAY_KEY_SECRET", "3qRCB5rpyy6J3WvmNFpWvk0N")

# Each payment grants 1 credit costing ₹50
CREDIT_PRICE_PAISE = 5000  # 50 INR in paise
CREDITS_PER_PAYMENT = 2
DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is not set")
engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True
)


SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="auth/google")
razorpay_client = razorpay.Client(auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET))

app = FastAPI(title="College Timetable Predictor API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],   # loosen for dev; restrict in prod
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ======================
# DB MODELS
# ======================

class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String, unique=True, index=True)
    google_sub = Column(String, unique=True, index=True, nullable=True)

    credits = Column(Integer, default=0)
    has_used_trial = Column(Boolean, default=False)

    # lock a Google account to a single device
    current_device_id = Column(String, nullable=True)

    reviews = relationship("Review", back_populates="user")



class Faculty(Base):
    __tablename__ = "faculties"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, unique=True, index=True)

    reviews = relationship("Review", back_populates="faculty")


class Review(Base):
    __tablename__ = "reviews"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    faculty_id = Column(Integer, ForeignKey("faculties.id"))
    rating = Column(Float)
    comment = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    # New: course context
    course_code = Column(String, nullable=True)
    course_title = Column(String, nullable=True)

    user = relationship("User", back_populates="reviews")
    faculty = relationship("Faculty", back_populates="reviews")

    __table_args__ = (
        UniqueConstraint("user_id", "faculty_id", name="uix_user_faculty"),
    )


@app.on_event("startup")
def startup():
    Base.metadata.create_all(bind=engine)


# ======================
# AUTH MODELS & HELPERS
# ======================

class Token(BaseModel):
    access_token: str
    token_type: str


class TokenData(BaseModel):
    email: Optional[str] = None


class UserOut(BaseModel):
    id: int
    email: EmailStr
    credits: int
    has_used_trial: bool

    class Config:
        from_attributes = True  # Pydantic v2


class GoogleAuthIn(BaseModel):
    id_token: str
    device_id: Optional[str] = None



def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def create_access_token(data: dict, expires_delta: Optional[timedelta] = None):
    to_encode = data.copy()
    expire = datetime.utcnow() + (expires_delta or timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES))
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


def get_user_by_email(db: Session, email: str) -> Optional[User]:
    return db.query(User).filter(User.email == email).first()


def get_user_by_google_sub(db: Session, sub: str) -> Optional[User]:
    return db.query(User).filter(User.google_sub == sub).first()


async def get_current_user(
    token: str = Depends(oauth2_scheme),
    db: Session = Depends(get_db),
) -> User:
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate login. Please sign in again.",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        email: str = payload.get("sub")
        if email is None:
            raise credentials_exception
        token_data = TokenData(email=email)
    except JWTError:
        raise credentials_exception

    user = get_user_by_email(db, token_data.email)
    if user is None:
        raise credentials_exception
    return user
def normalize_faculty_name(name: str) -> str:
    return name.strip().upper()
# ======================
# TIMETABLE CONSTANTS
# ======================

DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]

# 1h slots -> 4 big periods
PERIODS = {
    ("08:00", "09:00"): "P1",
    ("09:00", "10:00"): "P1",
    ("10:00", "11:00"): "P2",
    ("11:00", "12:00"): "P2",
    ("13:00", "14:00"): "P3",
    ("14:00", "15:00"): "P3",
    ("15:00", "16:00"): "P4",
    ("16:00", "17:00"): "P4",
}

# ======================
# API MODELS (TIMETABLE)
# ======================

class Section(BaseModel):
    section_code: str
    course_name: str
    faculty_name: str
    time_slots: Dict[str, List[str]]
    faculty_rating: Optional[float] = None


class Timetable(BaseModel):
    sections: List[Section]


class Preferences(BaseModel):
    dislike_early: bool = False
    dislike_midmorning: bool = False
    dislike_afternoon: bool = False
    dislike_evening: bool = False

    prefer_weekend_off: bool = True

    preferred_faculty: List[str] = []
    avoid_faculty: List[str] = []

    faculty_weight: float = 0.6
    free_days_weight: float = 0.2
    timing_weight: float = 0.2


class GenerateRequest(BaseModel):
    raw_text: str
    chosen_courses: List[str]
    preferences: Preferences = Preferences()
    top_k: int = 5


class ReviewIn(BaseModel):
    faculty_name: str
    rating: float  # 1–5
    comment: Optional[str] = None
    course_code: Optional[str] = None
    course_title: Optional[str] = None



class CoursesRequest(BaseModel):
    raw_text: str


class SectionSummary(BaseModel):
    section_code: str
    course_name: str
    faculty_name: str


class CourseSummary(BaseModel):
    course_name: str
    sections: List[SectionSummary]


class GenerateResponseItem(BaseModel):
    score: float
    timetable: Timetable
    grid: Dict[str, Dict[str, List[Dict[str, str]]]]


class OrderCreateOut(BaseModel):
    order_id: str
    amount: int
    currency: str
    key_id: str


class PaymentVerifyIn(BaseModel):
    razorpay_order_id: str
    razorpay_payment_id: str
    razorpay_signature: str

class ReviewOut(BaseModel):
    rating: float
    comment: Optional[str]
    created_at: datetime
    course_code: Optional[str] = None
    course_title: Optional[str] = None



class FacultySummaryOut(BaseModel):
    faculty_name: str
    avg_rating: float
    count: int
    summary: str
    breakdown: Dict[int, int]
    reviews: List[ReviewOut]

# ======================
# TIMETABLE PARSER
# ======================

def parse_sections(raw_text: str) -> List[Section]:
    # -------------------------
    # Preprocess
    # -------------------------
    def clean(line: str) -> str:
        line = line.strip()
        # Fix merged time ranges: 11:0012:00 → 11:00 12:00
        line = re.sub(r"(\d{2}:\d{2})(\d{2}:\d{2})", r"\1 \2", line)
        return line

    lines = [clean(l) for l in raw_text.splitlines() if clean(l)]

    sections: List[Section] = []

    current_course: Optional[str] = None
    current_section = None
    time_slots = None

    # -------------------------
    # Heuristics
    # -------------------------
    def looks_like_course_name(line: str) -> bool:
        if line.startswith(("UG -", "PG -")):
            return False
        if re.search(r"\d{2}:\d{2}", line):
            return False
        if any(x in line.lower() for x in ["date", "credits"]):
            return False
        if line.isupper() and "-" in line:
            return False
        return len(line.split()) >= 2

    def looks_like_section(line: str) -> bool:
        return line.startswith(("UG -", "PG -")) and "," in line and "-" in line

    TIME_RE = re.compile(r"(\d{2}:\d{2})\s*-\s*(\d{2}:\d{2})")

    def flush():
        nonlocal current_section, time_slots
        if current_course and current_section:
            if any(time_slots[d] for d in DAYS):
                sections.append(
                    Section(
                        section_code=current_section["code"],
                        course_name=current_course,
                        faculty_name=current_section["faculty"],
                        time_slots=time_slots,
                        faculty_rating=None,
                    )
                )
        current_section = None
        time_slots = None

    # -------------------------
    # Main parse loop
    # -------------------------
    i = 0
    while i < len(lines):
        line = lines[i]

        # ---- Course name detection ----
        if looks_like_course_name(line):
            flush()
            current_course = line
            i += 1
            continue

        # ---- Section header ----
        if looks_like_section(line):
            flush()

            parts = [p.strip() for p in line.split(",")]
            section_code = parts[1] if len(parts) > 1 else parts[0]

            # faculty = last "-" part
            faculty = line.split("-")[-1].strip()

            current_section = {
                "code": section_code,
                "faculty": faculty,
            }
            time_slots = {d: [] for d in DAYS}
            i += 1
            continue

        # ---- Day + Time lines ----
        if current_section:
            for day in DAYS:
                if line.startswith(day):
                    ranges = TIME_RE.findall(line)

                    # Two 1-hour slots → ONE period
                    periods_seen = set()
                    for start, end in ranges:
                        if (start, end) in PERIODS:
                            periods_seen.add(PERIODS[(start, end)])

                    for p in periods_seen:
                        if p not in time_slots[day]:
                            time_slots[day].append(p)
                    break

        i += 1

    flush()
    return sections






# ======================
# SCORING HELPERS
# ======================

def clashes_with_current(current_sections: List[Section], new_section: Section) -> bool:
    occupied = set()
    for s in current_sections:
        for day, periods in s.time_slots.items():
            for p in periods:
                occupied.add((day, p))
    for day, periods in new_section.time_slots.items():
        for p in periods:
            if (day, p) in occupied:
                return True
    return False


def occupied_slots(sections: List[Section]) -> Dict[tuple, Section]:
    occ: Dict[tuple, Section] = {}
    for s in sections:
        for day, periods in s.time_slots.items():
            for p in periods:
                key = (day, p)
                if key in occ:
                    raise ValueError("Clash detected")
                occ[key] = s
    return occ


def free_days_score(occ: Dict[tuple, Section]) -> float:
    score = 0.0
    for day in DAYS:
        if not any(k[0] == day for k in occ.keys()):
            score += 1.0
            if day == "Saturday":
                score += 1.0
    return score


def timing_penalty(occ: Dict[tuple, Section], prefs: Preferences) -> float:
    penalty = 0.0
    for (_day, period) in occ.keys():
        if prefs.dislike_early and period == "P1":
            penalty -= 1.0
        if prefs.dislike_midmorning and period == "P2":
            penalty -= 1.0
        if prefs.dislike_afternoon and period == "P3":
            penalty -= 1.0
        if prefs.dislike_evening and period == "P4":
            penalty -= 1.0
    return penalty


def get_faculty_rating_db(db: Session, faculty_name: str) -> float:
    faculty_name = normalize_faculty_name(faculty_name)
    faculty = db.query(Faculty).filter(Faculty.name == faculty_name).first()
    if not faculty:
        return 3.5
    avg = db.query(func.avg(Review.rating)).filter(Review.faculty_id == faculty.id).scalar()
    if avg is None:
        return 3.5
    return float(avg)


def faculty_preference_score(sections: List[Section], prefs: Preferences) -> float:
    score = 0.0
    for sec in sections:
        if sec.faculty_name in prefs.preferred_faculty:
            score += 2.0
        if sec.faculty_name in prefs.avoid_faculty:
            score -= 3.0
    return score


def score_timetable(sections: List[Section], prefs: Preferences) -> float:
    try:
        occ = occupied_slots(sections)
    except ValueError:
        return -1e9

    ratings = [s.faculty_rating or 3.5 for s in sections]
    faculty_score = sum(ratings) / len(ratings) if ratings else 0.0
    free_score = free_days_score(occ) if prefs.prefer_weekend_off else 0.0
    time_pen = timing_penalty(occ, prefs)
    faculty_pref = faculty_preference_score(sections, prefs)

    return (
        prefs.faculty_weight * faculty_score +
        prefs.free_days_weight * free_score +
        prefs.timing_weight * time_pen +
        faculty_pref
    )


def group_by_course(sections: List[Section]) -> Dict[str, List[Section]]:
    by_course: Dict[str, List[Section]] = defaultdict(list)
    for s in sections:
        by_course[s.course_name].append(s)
    return by_course


def build_grid(sections: List[Section]) -> Dict[str, Dict[str, List[Dict[str, str]]]]:
    grid: Dict[str, Dict[str, List[Dict[str, str]]]] = {
        day: {p: [] for p in ["P1", "P2", "P3", "P4"]} for day in DAYS
    }
    for sec in sections:
        for day, periods in sec.time_slots.items():
            for p in periods:
                grid[day][p].append({
                    "course": sec.course_name,
                    "faculty": sec.faculty_name,
                    "section": sec.section_code,
                })
    return grid


def build_best_timetables(
    sections: List[Section],
    chosen_courses: List[str],
    prefs: Preferences,
    top_k: int = 5,
) -> List[GenerateResponseItem]:
    by_course = group_by_course(sections)
    filtered_courses = [c for c in chosen_courses if c in by_course]

    best: List[tuple[float, List[Section]]] = []

    def backtrack(i: int, current_sections: List[Section]):
        nonlocal best
        if i == len(filtered_courses):
            sc = score_timetable(current_sections, prefs)
            best.append((sc, list(current_sections)))
            best.sort(key=lambda x: x[0], reverse=True)
            if len(best) > top_k:
                best[:] = best[:top_k]
            return

        course = filtered_courses[i]
        for sec in by_course[course]:
            if clashes_with_current(current_sections, sec):
                continue
            current_sections.append(sec)
            backtrack(i + 1, current_sections)
            current_sections.pop()

    backtrack(0, [])

    results: List[GenerateResponseItem] = []
    for score, secs in best:
        results.append(GenerateResponseItem(
            score=score,
            timetable=Timetable(sections=secs),
            grid=build_grid(secs),
        ))
    return results

# ======================
# FACULTY REVIEW SUMMARY (Amazon-style)
# ======================

def build_faculty_summary(faculty_name: str, reviews: List[Review]) -> FacultySummaryOut:
    if not reviews:
        return FacultySummaryOut(
            faculty_name=faculty_name,
            avg_rating=0.0,
            count=0,
            summary="No student reviews yet. Be the first to share your experience.",
            breakdown={i: 0 for i in range(1, 6)},
            reviews=[],
        )

    ratings = [int(round(r.rating)) for r in reviews]
    avg = sum(ratings) / len(ratings)

    breakdown = {i: 0 for i in range(1, 6)}
    for r in ratings:
        if 1 <= r <= 5:
            breakdown[r] += 1

    # Amazon-like summary text
    if avg >= 4.5:
        tone = "Students consistently rate this faculty as excellent with highly positive feedback."
    elif avg >= 4.0:
        tone = "Students generally have a very good experience with this faculty."
    elif avg >= 3.0:
        tone = "Feedback is mixed: some students are satisfied, while others see room for improvement."
    elif avg > 0:
        tone = "Students often find this faculty challenging, with several critical comments."
    else:
        tone = "No clear trend from reviews yet."

    out_reviews = [
        ReviewOut(
            rating=r.rating,
            comment=r.comment,
            created_at=r.created_at,
            course_code=r.course_code,
            course_title=r.course_title,
        )
        for r in sorted(reviews, key=lambda x: x.created_at, reverse=True)
    ]

    return FacultySummaryOut(
        faculty_name=faculty_name,
        avg_rating=avg,
        count=len(reviews),
        summary=tone,
        breakdown=breakdown,
        reviews=out_reviews,
    )

# ======================
# AUTH ENDPOINTS (GOOGLE LOGIN ONLY)
# ======================

@app.post("/auth/google", response_model=Token)
def google_login(body: GoogleAuthIn, db: Session = Depends(get_db)):
    token = body.id_token
    device_id = body.device_id or ""

    try:
        idinfo = id_token.verify_oauth2_token(
            token,
            grequests.Request(),
            GOOGLE_CLIENT_ID
        )
        email = idinfo["email"]
        sub = idinfo["sub"]
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid Google token. One device should have one account")

    # ✅ Enforce: ONE DEVICE → ONE GOOGLE ACCOUNT
    if device_id:
        existing_user = (
            db.query(User)
            .filter(User.current_device_id == device_id)
            .first()
        )
        if existing_user and existing_user.google_sub != sub:
            raise HTTPException(
                status_code=403,
                detail="This device is already linked to another Google account."
            )

    # Find or create user
    user = get_user_by_google_sub(db, sub)
    if not user:
        user = get_user_by_email(db, email)
        if user and not user.google_sub:
            user.google_sub = sub
        elif not user:
            user = User(
                email=email,
                google_sub=sub,
                credits=0,
                has_used_trial=False,
            )
            db.add(user)

        db.commit()
        db.refresh(user)

    # ✅ Bind device to user (only once)
    if device_id and not user.current_device_id:
        user.current_device_id = device_id
        db.add(user)
        db.commit()
        db.refresh(user)

    access_token = create_access_token(data={"sub": user.email})
    return {"access_token": access_token, "token_type": "bearer"}




@app.get("/auth/me", response_model=UserOut)
def read_users_me(current_user: User = Depends(get_current_user)):
    return current_user

# ======================
# PAYMENT / CREDIT ENDPOINTS (RAZORPAY)
# ======================

@app.post("/payments/create-order", response_model=OrderCreateOut)
def create_order(current_user: User = Depends(get_current_user)):
    order = razorpay_client.order.create(dict(
        amount=CREDIT_PRICE_PAISE,
        currency="INR",
        payment_capture=1
    ))
    return OrderCreateOut(
        order_id=order["id"],
        amount=CREDIT_PRICE_PAISE,
        currency="INR",
        key_id=RAZORPAY_KEY_ID
    )


@app.post("/payments/verify")
def verify_payment(
    data: PaymentVerifyIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    body = data.razorpay_order_id + "|" + data.razorpay_payment_id
    expected_signature = hmac.new(
        RAZORPAY_KEY_SECRET.encode(),
        body.encode(),
        hashlib.sha256
    ).hexdigest()

    if expected_signature != data.razorpay_signature:
        raise HTTPException(status_code=400, detail="Invalid payment signature")

    current_user.credits += CREDITS_PER_PAYMENT
    db.add(current_user)
    db.commit()
    db.refresh(current_user)

    return {"message": "Payment verified, 3 credit added", "credits": current_user.credits}

# ======================
# TIMETABLE API ENDPOINTS
# ======================

@app.post("/courses", response_model=List[CourseSummary])
def get_courses(req: CoursesRequest):
    sections = parse_sections(req.raw_text)
    by_course = group_by_course(sections)
    result: List[CourseSummary] = []
    for course_name, secs in by_course.items():
        result.append(CourseSummary(
            course_name=course_name,
            sections=[
                SectionSummary(
                    section_code=s.section_code,
                    course_name=s.course_name,
                    faculty_name=s.faculty_name,
                )
                for s in secs
            ],
        ))
    return result


def charge_credit_if_needed(db: Session, user: User):
    """
    First generate is free (trial).
    After that, every /generate consumes 1 credit.
    """
    if not user.has_used_trial:
        user.has_used_trial = True
    else:
        if user.credits <= 0:
            raise HTTPException(
                status_code=402,
                detail="You have no credits left. Please purchase a credit to generate a new timetable."
            )
        user.credits -= 1
    db.add(user)
    db.commit()
    db.refresh(user)


@app.post("/generate", response_model=List[GenerateResponseItem])
def generate(
    req: GenerateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    sections = parse_sections(req.raw_text)

    for s in sections:
        s.faculty_rating = get_faculty_rating_db(db, s.faculty_name)

    results = build_best_timetables(
        sections,
        req.chosen_courses,
        req.preferences,
        req.top_k
    )

    # ❌ NO VALID TIMETABLE → NO CREDIT CHARGED
    if not results:
        raise HTTPException(
            status_code=400,
            detail="No valid timetable found without clashes. Try changing preferences or courses."
        )

    # ✅ VALID RESULT → NOW CHARGE CREDIT
    charge_credit_if_needed(db, current_user)

    return results

# ======================
# FACULTY REVIEWS API
# ======================
@app.get("/faculty/search")
def search_faculty(q: str, db: Session = Depends(get_db)):
    if not q or len(q.strip()) < 2:
        return []

    q = q.strip().upper()

    faculties = (
        db.query(Faculty)
        .filter(Faculty.name.contains(q))
        .order_by(Faculty.name)
        .limit(10)
        .all()
    )

    return [
        {
            "id": f.id,
            "name": f.name
        }
        for f in faculties
    ]
@app.get("/faculty/{faculty_id}/courses")
def get_faculty_courses(faculty_id: int, db: Session = Depends(get_db)):
    courses = (
        db.query(
            Review.course_code,
            Review.course_title
        )
        .filter(
            Review.faculty_id == faculty_id,
            Review.course_code.isnot(None)
        )
        .distinct()
        .all()
    )

    return [
        {
            "course_code": c.course_code,
            "course_title": c.course_title
        }
        for c in courses
    ]

@app.post("/review")
def submit_review(
    review: ReviewIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if review.rating < 1 or review.rating > 5:
        raise HTTPException(status_code=400, detail="Rating must be between 1 and 5")

    faculty_name = normalize_faculty_name(review.faculty_name)

    faculty = db.query(Faculty).filter(Faculty.name == faculty_name).first()
    if not faculty:
        faculty = Faculty(name=faculty_name)
        db.add(faculty)
        db.commit()
        db.refresh(faculty)

    existing = (
        db.query(Review)
        .filter(
            Review.user_id == current_user.id,
            Review.faculty_id == faculty.id
        )
        .first()
    )

    if existing:
        existing.rating = review.rating
        existing.comment = review.comment
        existing.course_code = review.course_code
        existing.course_title = review.course_title
        existing.created_at = datetime.utcnow()
    else:
        db.add(Review(
            user_id=current_user.id,
            faculty_id=faculty.id,
            rating=review.rating,
            comment=review.comment,
            course_code=review.course_code,
            course_title=review.course_title,
        ))

    db.commit()

    avg = get_faculty_rating_db(db, faculty.name)
    return {
        "message": "Review recorded (anonymous)",
        "faculty_name": faculty.name,
        "avg_rating": avg,
    }


@app.get("/faculty/{faculty_name}/reviews", response_model=FacultySummaryOut)
def get_faculty_reviews(faculty_name: str, db: Session = Depends(get_db)):
    faculty_name = normalize_faculty_name(faculty_name)
    faculty = db.query(Faculty).filter(Faculty.name == faculty_name).first()
    if not faculty:
        return FacultySummaryOut(
            faculty_name=faculty_name,
            avg_rating=0.0,
            count=0,
            summary="No student reviews yet for this faculty.",
            breakdown={i: 0 for i in range(1, 6)},
            reviews=[],
        )

    reviews = db.query(Review).filter(Review.faculty_id == faculty.id).all()
    return build_faculty_summary(faculty_name, reviews)

# ======================
# FRONTEND ROUTE
# ======================

@app.get("/", response_class=HTMLResponse)
def serve_frontend():
    return FileResponse("index.html")






















