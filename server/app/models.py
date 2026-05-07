from datetime import datetime, timezone
from sqlalchemy import Integer, Text, Float, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column, relationship
from .database import Base


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class File(Base):
    __tablename__ = "files"

    blake3_hash: Mapped[str] = mapped_column(Text, primary_key=True)
    file_size: Mapped[int] = mapped_column(Integer, nullable=False)
    first_seen: Mapped[str] = mapped_column(Text, nullable=False, default=_now)
    last_verified: Mapped[str | None] = mapped_column(Text, nullable=True)
    verify_status: Mapped[str] = mapped_column(Text, nullable=False, default="seen")
    # 'seen' | 'verified' | 'missing' | 'error'

    locations: Mapped[list["Location"]] = relationship(back_populates="file")


class Location(Base):
    __tablename__ = "locations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    blake3_hash: Mapped[str] = mapped_column(Text, ForeignKey("files.blake3_hash"), nullable=False)
    file_path: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    last_seen: Mapped[str] = mapped_column(Text, nullable=False, default=_now)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="present")
    # 'present' | 'missing'

    file: Mapped["File"] = relationship(back_populates="locations")


class StorageRoot(Base):
    __tablename__ = "storage_roots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    path: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    label: Mapped[str | None] = mapped_column(Text, nullable=True)
    added_at: Mapped[str] = mapped_column(Text, nullable=False, default=_now)


class VerificationEvent(Base):
    __tablename__ = "verification_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    blake3_hash: Mapped[str] = mapped_column(Text, nullable=False)
    queried_at: Mapped[str] = mapped_column(Text, nullable=False, default=_now)
    result: Mapped[str] = mapped_column(Text, nullable=False)
    client_host: Mapped[str | None] = mapped_column(Text, nullable=True)
