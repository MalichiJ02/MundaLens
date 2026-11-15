"""Compare grower-selected rotations using published crop water ranges."""

from pydantic import BaseModel, Field, field_validator, model_validator
from engine import evidence_checks, rank_crops

WATER_SOURCE = "https://www.fao.org/4/s2022e/s2022e07.htm"
STAGE_SOURCE = "https://www.fao.org/4/t7202e/t7202e05.htm"
CROPS = {
    "Maize": (500, 800, "medium to high", "flowering and grain filling"),
    "Groundnut": (500, 700, "low to medium", "flowering and pod setting"),
    "Soybean": (450, 700, "low to medium", "flowering and yield formation"),
    "Sorghum": (450, 650, "low", "flowering and yield formation"),
    "Dry bean": (300, 500, "medium to high", "flowering and pod filling"),
    "Cowpea": (400, 700, "low to medium", "flowering and pod filling"),
    "Sunflower": (600, 1000, "low to medium", "flowering and seed filling"),
    "Sweet potato": (450, 650, "medium", "storage root formation"),
}
LEGUMES = {"Groundnut", "Soybean", "Dry bean", "Cowpea"}


class RotationOptions(BaseModel):
    previous: str
    alternatives: list[str] = Field(min_length=2, max_length=2)
    irrigation: str = Field(pattern="^(rainfed|irrigated|unknown)$")
    priority: str = Field(pattern="^(water|cover|market)$")
    soil: str = Field(pattern="^(dry|moist|wet|unknown)$")
    local_notes: str = Field(default="", max_length=360)
    soil_ph: float | None = Field(default=None, ge=3, le=10)

    @field_validator("previous")
    @classmethod
    def known_previous(cls, value):
        if value not in CROPS:
            raise ValueError("Choose a crop from the current reference list.")
        return value

    @field_validator("alternatives")
    @classmethod
    def known_alternatives(cls, values):
        if len(set(values)) != 2 or any(value not in CROPS for value in values):
            raise ValueError("Choose two different alternative crops.")
        return values

    @model_validator(mode="after")
    def distinct_choices(self):
        if self.previous in self.alternatives:
            raise ValueError("Choose alternatives that differ from the current crop.")
        return self


def compare_rotations(history, options, lat=0.0, context=None):
    complete = history["valid"] == len(history["days"])
    rainfall = history["total"] if complete else None
    scenarios = []
    for crop in [options.previous, *options.alternatives]:
        low, high, sensitivity, stage = CROPS[crop]
        if rainfall is None:
            water = "Daily rainfall coverage is incomplete for the selected period."
        elif rainfall < low:
            water = f"Historical rainfall was below the {low} mm low end of the indicative crop water range."
        elif rainfall <= high:
            water = "Historical rainfall was within the indicative crop water range."
        else:
            water = f"Historical rainfall was above the {high} mm high end of the indicative crop water range."
        scenarios.append({
            "crop": crop, "sequence": [options.previous, crop], "water_mm": [low, high],
            "sensitivity": sensitivity, "stage": stage, "water_context": water,
            "legume": crop in LEGUMES,
        })
    context = context or {}
    spin = context.get("spin_days") or []
    ranking = rank_crops(spin + history["days"], lat, options.previous, options.irrigation,
                         options.soil, options.priority, options.soil_ph,
                         soil_profile=context.get("soil_profile"), slope=context.get("slope"),
                         elevation=context.get("elevation"), window_start=len(spin),
                         past_seasons=context.get("past_seasons") or ())
    plant_index = ranking.pop("_plant_index")
    profile = context.get("soil_profile")
    evidence = dict(context.get("evidence") or {})
    if profile and evidence.get("smap"):
        evidence["smap"] = dict(evidence["smap"], range=(profile["wilting_point"], profile["field_capacity"]))
    ranking["checks"] = evidence_checks(
        spin + history["days"], lat, options.previous, plant_index,
        profile["awc_mm_per_m"] if profile else None, options.soil, options.irrigation == "irrigated",
        context.get("slope"), context.get("elevation") or 0.0, len(spin),
        smap=evidence.get("smap"), hls=evidence.get("hls"))
    by_crop = {item["crop"]: item for item in ranking["ranked"]}
    for scenario in scenarios:
        scenario["assessment"] = by_crop[scenario["crop"]]
    recommended = [item for item in ranking["ranked"] if item["crop"] != options.previous][:2]
    ranking["recommended"] = recommended
    ranking["current"] = by_crop[options.previous]
    return {"scenarios": scenarios, "ranking": ranking, "water_source": WATER_SOURCE, "stage_source": STAGE_SOURCE}


assert set(CROPS) == set(__import__("engine").CROP_DB), "CROPS and engine.CROP_DB must list the same crops"
