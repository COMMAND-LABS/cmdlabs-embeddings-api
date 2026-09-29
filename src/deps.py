import logging
from typing import Annotated
from sqlalchemy.orm import Session
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from passlib.context import CryptContext
from jose import jwt, JWTError
from dotenv import load_dotenv
import os
from .db.database import SessionLocal
from fastapi import Request
from .db.models import ApiKey, Account, ApiKeyStatus
from sqlalchemy import func

load_dotenv()

logger = logging.getLogger(__name__)

SECRET_KEY = os.getenv('AUTH_SECRET_KEY')
ALGORITHM = os.getenv('AUTH_ALGORITHM')

def get_db():
    """Database session dependency"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

db_dependency = Annotated[Session, Depends(get_db)]
bcrypt_context = CryptContext(schemes=["sha256_crypt"])

async def get_current_user(request: Request):
    """
    Legacy JWT-only authentication.
    Validates JWT token from Authorization Bearer header.
    """
    try:
        # Extract token from Authorization Bearer header
        authorization = request.headers.get("Authorization")

        
        if not authorization:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
        
        # Extract token from "Bearer <token>" format


        parts = authorization.split(" ")
        if len(parts) != 2 or parts[0].lower() != "bearer":
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid authorization header format")
        
        token = parts[1]


        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])


        email: str | None = payload.get('sub')
        account_id: str = payload.get('id')
        
        if email is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail='Could not validate user')
        
        return {'username': email, 'id': account_id}
    except JWTError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail='Could not validate user')
    
jwt_dependency = Annotated[dict, Depends(get_current_user)]


async def get_current_user_or_api_key(
    request: Request,
    db: Session = Depends(get_db)
) -> dict:
    """
    Unified authentication: tries JWT first, then API key.
    Returns same format: {'email': str, 'id': int, 'auth_type': 'jwt'|'api_key'}
    
    This allows the Embeddings API to accept authentication from both:
    1. JWT tokens (for direct user access)
    2. API keys (for service-to-service calls from the Kalygo AI API)
    """
    # Try JWT first (existing flow)
    try:
        authorization = request.headers.get("Authorization", "")
        
        
        # Check if it's a Bearer token (JWT format)
        if authorization.startswith("Bearer "):
            token = authorization.replace("Bearer ", "").strip()
            
            
            # Try to decode as JWT
            try:
                payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
                email = payload.get('sub')
                account_id = payload.get('id')
                
                if email:
                    return {
                        'email': email,
                        'id': int(account_id) if isinstance(account_id, str) else account_id,
                        'auth_type': 'jwt'
                    }
            except JWTError as e:
                # Not a valid JWT, might be an API key
                pass
            except (KeyError, ValueError) as e:
                pass
    except Exception as e:
        pass
    
    # Try API key from headers
    api_key = None
    
    # Check Authorization header: "Bearer kalygo_live_..."
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        potential_key = auth_header.replace("Bearer ", "").strip()
        if potential_key.startswith("kalygo_"):
            api_key = potential_key
    
    # Also check X-API-Key header
    if not api_key:
        x_api_key = request.headers.get("X-API-Key", "").strip()
        if x_api_key.startswith("kalygo_"):
            api_key = x_api_key
    
    if api_key:
        
        # Extract prefix for fast lookup
        key_prefix = api_key[:20] if len(api_key) >= 20 else api_key
        
        # Query by prefix first (fast), then verify hash
        api_key_record = db.query(ApiKey).filter(
            ApiKey.key_prefix == key_prefix,
            ApiKey.status == ApiKeyStatus.ACTIVE
        ).first()
        
        if api_key_record:
            
            # Verify the full key against hash
            from .utils.api_key_utils import verify_api_key
            
            
            if verify_api_key(api_key, api_key_record.key_hash):
                # Update last_used_at
                api_key_record.last_used_at = func.now()
                db.commit()
                
                # Get account email
                account = db.query(Account).filter(Account.id == api_key_record.account_id).first()
                if account:
                    return {
                        'email': account.email,
                        'id': api_key_record.account_id,
                        'auth_type': 'api_key',
                        'api_key_id': api_key_record.id  # Useful for logging
                    }
    
    # No valid auth found. Log THAT it failed, never the header, token, key,
    # hash, secret or email: these print()s used to write all of those to the
    # service logs on every request.
    logger.info("Embeddings auth failed: no valid JWT or API key")
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Authentication required. Provide JWT token or API key in Authorization/X-API-Key header."
    )


# New unified dependency for routes that should accept both JWT and API key
auth_dependency = Annotated[dict, Depends(get_current_user_or_api_key)]