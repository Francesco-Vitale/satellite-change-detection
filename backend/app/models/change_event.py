"""ORM model for the change_events table.

Schema matches the brief exactly:
    change_events(id, tile_id, date_before, date_after, bbox_geojson,
                   change_area_km2, severity_score, created_at)
"""
from datetime import date, datetime

from sqlalchemy import Date, DateTime, Float, Integer, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


class ChangeEvent(Base):
    __tablename__ = "change_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tile_id: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    date_before: Mapped[date] = mapped_column(Date, nullable=False)
    date_after: Mapped[date] = mapped_column(Date, nullable=False)

    # GeoJSON Feature / FeatureCollection of the changed-pixel polygons,
    # stored as JSONB so it can be queried/indexed if needed later.
    bbox_geojson: Mapped[dict] = mapped_column(JSONB, nullable=False)

    change_area_km2: Mapped[float] = mapped_column(Float, nullable=False)
    # Mean absolute NDVI delta over changed pixels, normalized 0-1.
    severity_score: Mapped[float] = mapped_column(Float, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return (
            f"<ChangeEvent id={self.id} tile={self.tile_id} "
            f"{self.date_before}->{self.date_after} "
            f"area={self.change_area_km2:.2f}km2 severity={self.severity_score:.2f}>"
        )
