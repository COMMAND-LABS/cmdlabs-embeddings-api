from fastapi import APIRouter, Response, HTTPException
from sentence_transformers import SentenceTransformer
import logging
import os
import threading
import time

from src.core.schemas.EmbeddingInput import EmbeddingInput
from src.deps import jwt_dependency, auth_dependency

# Configure logging to reduce verbose output
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Reduce verbose output from sentence_transformers
logging.getLogger("sentence_transformers").setLevel(logging.WARNING)
logging.getLogger("transformers").setLevel(logging.WARNING)
logging.getLogger("torch").setLevel(logging.WARNING)

router = APIRouter(
    prefix='/huggingface',
    tags=['huggingface']
)

# Global model instance with proper locking
_embedding_model = None
_model_loading_lock = threading.Lock()
_last_loading_attempt = 0
_loading_cooldown = 30  # seconds

def get_embedding_model():
    """Get or create the embedding model instance with retry logic"""
    global _embedding_model, _last_loading_attempt
    
    # If model is already loaded, return it
    if _embedding_model is not None:
        return _embedding_model
    
    # Check if we're in cooldown period
    current_time = time.time()
    if current_time - _last_loading_attempt < _loading_cooldown:
        raise HTTPException(
            status_code=503,
            detail="Model is currently loading. Please try again in a few seconds."
        )
    
    # Use lock to prevent multiple simultaneous loading attempts
    with _model_loading_lock:
        # Double-check if model was loaded while waiting for lock
        if _embedding_model is not None:
            return _embedding_model
        
        _last_loading_attempt = current_time
        
        try:
            logger.info("Loading all-MiniLM-L6-v2 model...")
            # Load model with explicit CPU device and proper error handling
            _embedding_model = SentenceTransformer('sentence-transformers/all-MiniLM-L6-v2')
            
            # Force model to CPU to avoid any device issues
            if hasattr(_embedding_model, 'to'):
                _embedding_model.to('cpu')
            
            logger.info("✅ Model loaded successfully and cached in memory")
            return _embedding_model
            
        except Exception as e:
            logger.error(f"Failed to load model: {e}")
            # Reset the model to None so we can retry
            _embedding_model = None
            raise HTTPException(
                status_code=500, 
                detail=f"Failed to load embedding model. Please try again. Error: {str(e)}"
            )

@router.post("/embedding")
def create_embeddings(response: Response, textToEmbed: EmbeddingInput, user: auth_dependency):
    # No logging of the caller or the input: the input is customer document
    # text, and the print()s here used to write it (and the user) to the logs
    # on every call.
    try:
        embedding_model = get_embedding_model()
        # Generate embeddings - ensure input is a list
        embeddings = embedding_model.encode([textToEmbed.input], show_progress_bar=False)
        return {"embedding": embeddings.tolist(), "model": "sentence-transformers/all-MiniLM-L6-v2"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error generating embeddings: %s", type(e).__name__)
        raise HTTPException(status_code=500, detail="Failed to generate embeddings")

@router.get("/health")
def health_check():
    """Health check endpoint to verify model status"""
    try:
        model = get_embedding_model()
        return {
            "status": "healthy",
            "model": "sentence-transformers/all-MiniLM-L6-v2",
            "model_loaded": model is not None
        }
    except Exception as e:
        return {
            "status": "unhealthy",
            "error": str(e),
            "model_loaded": False
        }