"""Physical product inputs: spec file, CAD/dimension exports, photos, similar products."""

from __future__ import annotations

import csv
import struct
from pathlib import Path
from typing import Any

import numpy as np
from pydantic import BaseModel, Field

from proofloop.core.config import ConfigError, load_yaml_file


class SimilarProduct(BaseModel):
    name: str
    spec: dict[str, Any] = Field(default_factory=dict)


class ProductSpec(BaseModel):
    name: str
    category: str = ""
    spec: dict[str, Any] = Field(default_factory=dict)       # e.g. weight_g, price_usd, dimensions_mm
    photos: list[str] = Field(default_factory=list)
    cad: list[str] = Field(default_factory=list)             # STL files or dimension CSVs
    reviews: list[str] = Field(default_factory=list)         # reviews of SIMILAR products
    similar_products: list[SimilarProduct] = Field(default_factory=list)
    sensors: list[str] = Field(default_factory=list)         # prototype sensor/usage CSVs
    base_dir: str = "."

    def path(self, rel: str) -> Path:
        p = Path(rel)
        return p if p.is_absolute() else Path(self.base_dir) / p


def load_product(path: Path | str) -> ProductSpec:
    path = Path(path)
    raw = load_yaml_file(path)
    if not raw:
        raise ConfigError(f"product file {path} is empty or missing")
    raw["base_dir"] = str(path.parent)
    product = ProductSpec.model_validate(raw)
    for cad in product.cad:
        cad_path = product.path(cad)
        dims = read_dimensions(cad_path)
        if cad_path.suffix.lower() == ".stl":  # bounding box -> outer dimensions (diameter for round products)
            dims.setdefault("diameter", max(dims["width"], dims["depth"]))
            product.spec.setdefault("dimensions_mm", {}).update(dims)
        else:  # named measurements, e.g. mouth_diameter_mm
            product.spec.update(dims)
    for photo in product.photos:
        if not product.path(photo).is_file():
            raise ConfigError(f"photo not found: {photo}")
    return product


# --------------------------------------------------------------------- CAD


def stl_bounding_box(path: Path) -> dict[str, float]:
    """Bounding box (mm) of an ASCII or binary STL."""
    data = path.read_bytes()
    if data[:5].lower() == b"solid" and b"facet" in data[:400]:
        verts = [list(map(float, line.split()[1:4])) for line in data.decode(errors="ignore").splitlines()
                 if line.strip().startswith("vertex")]
        pts = np.array(verts)
    else:
        count = struct.unpack("<I", data[80:84])[0]
        rec = np.frombuffer(data[84:84 + count * 50], dtype=np.dtype([("n", "<f4", 3), ("v", "<f4", (3, 3)),
                                                                        ("a", "<u2")]))
        pts = rec["v"].reshape(-1, 3)
    if pts.size == 0:
        raise ConfigError(f"no vertices in {path}")
    size = pts.max(axis=0) - pts.min(axis=0)
    x, y, z = (round(float(v), 1) for v in size)
    return {"width": x, "depth": y, "height": z}


def read_dimensions(path: Path) -> dict[str, float]:
    if path.suffix.lower() == ".stl":
        return stl_bounding_box(path)
    if path.suffix.lower() == ".csv":  # name,value_mm rows
        out = {}
        with path.open(newline="", encoding="utf-8-sig") as fh:
            for row in csv.reader(fh):
                if len(row) >= 2:
                    try:
                        out[row[0].strip()] = float(row[1])
                    except ValueError:
                        continue
        return out
    raise ConfigError(f"unsupported CAD/dimension file: {path}")


def flat_spec(spec: dict[str, Any], prefix: str = "") -> dict[str, float]:
    """Numeric spec values with dotted keys, e.g. {'dimensions_mm.height': 290}."""
    out: dict[str, float] = {}
    for key, value in spec.items():
        name = f"{prefix}{key}"
        if isinstance(value, dict):
            out.update(flat_spec(value, name + "."))
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            out[name] = float(value)
    return out
