from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.orm import Session
from sqlalchemy import text
from app.database import get_db, create_tenant_schema
from app.auth import get_password_hash, verify_password, create_access_token, get_current_user
from pydantic import BaseModel

router = APIRouter(prefix="/api/auth", tags=["Authentication"])

class RegisterTenantRequest(BaseModel):
    company_name: str
    username: str
    password: str

@router.post("/register")
def register_tenant(req: RegisterTenantRequest, db: Session = Depends(get_db)):
    schema_name = f"tenant_{req.company_name.lower().replace(' ', '_').replace('-', '_')}"

    existing = db.execute(text("SELECT id FROM public.tenants WHERE company_name = :c"), {"c": req.company_name}).fetchone()
    if existing:
        raise HTTPException(status_code=400, detail="Company name already registered")

    user_exist = db.execute(text("SELECT id FROM public.users WHERE username = :u"), {"u": req.username}).fetchone()
    if user_exist:
        raise HTTPException(status_code=400, detail="Username already exists")

    # Create Tenant & User
    t_res = db.execute(
        text("INSERT INTO public.tenants (company_name, schema_name) VALUES (:c, :s) RETURNING id"),
        {"c": req.company_name, "s": schema_name}
    )
    tenant_id = t_res.fetchone()[0]

    db.execute(
        text("INSERT INTO public.users (tenant_id, username, hashed_password, is_admin) VALUES (:t, :u, :p, true)"),
        {"t": tenant_id, "u": req.username, "p": get_password_hash(req.password)}
    )
    db.commit()

    # Create isolated PostgreSQL schema for this tenant
    create_tenant_schema(schema_name)

    return {"message": "Customer registered successfully", "tenant_id": tenant_id, "schema": schema_name}

@router.post("/login")
def login(form_data: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
    user = db.execute(
        text("""
            SELECT u.id, u.tenant_id, u.username, u.hashed_password, t.schema_name
            FROM public.users u
            JOIN public.tenants t ON t.id = u.tenant_id
            WHERE u.username = :u
        """),
        {"u": form_data.username}
    ).fetchone()

    if not user or not verify_password(form_data.password, user[3]):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Incorrect username or password")

    token = create_access_token(data={"sub": user[2], "tenant_id": user[1], "schema_name": user[4]})
    return {"access_token": token, "token_type": "bearer", "schema_name": user[4], "username": user[2]}

@router.get("/me")
def get_me(user: dict = Depends(get_current_user)):
    return user
