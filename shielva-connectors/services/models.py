"""
Credential Models
SQLAlchemy models for credential storage.
"""
from sqlalchemy import Column, String, DateTime, Text
from sqlalchemy.ext.declarative import declarative_base
import uuid
from datetime import datetime

Base = declarative_base()

class CredentialModel(Base):
    """
    SQLAlchemy model for storing encrypted credentials.
    """
    __tablename__ = "integration_credentials"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    tenant_id = Column(String, index=True, nullable=False)
    connector_type = Column(String, index=True, nullable=False)  # slack, jira, etc.
    encrypted_data = Column(Text, nullable=False)  # Base64 encoded ciphertext
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def __repr__(self):
        return f"<Credential(tenant={self.tenant_id}, type={self.connector_type})>"
