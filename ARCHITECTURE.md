# MundaLens architecture

The Python 3.10+ backend uses FastAPI, Uvicorn, Pydantic, NumPy, Rasterio, h5py and netCDF4. The frontend uses HTML, CSS, JavaScript and Leaflet. The browser sends the farm pin and selected dates. The backend retrieves NASA data, extracts values and returns JSON. The frontend starts evidence requests independently so a slow provider does not block weather history or crop comparison. Failed or unavailable observations are shown alongside successful sources.

## Application API

| Endpoint | Inputs | Output |
|---|---|---|
| GET /api/config | None | Supported crops and optional browser key for the Google basemap |
| POST /api/farm/analysis | Coordinates, dates, crop/field inputs, optional rainfall source, number of past seasons and SMAP/Landsat observations | Daily series, period total, longest dry spell, ranked crops (median and bad-year across past seasons), the inputs used, and consistency checks |
| GET /api/live/conditions | Coordinates | Latest NASA POWER rainfall and max temperature, plus an available-day count for the seven-day rainfall total |
| GET /api/live/imerg-map | Coordinates | Centered GPM IMERG precipitation layer, NASA catalog observation timestamp and map legend |
| GET /api/farm/satellite/imerg | Coordinates | Latest published daily Early Run granule date and grid-cell precipitation in mm when Earthdata is configured |
| GET /api/farm/satellite/smap | Coordinates | Latest SPL4SMGP V008 granule date and root-zone moisture when Earthdata is configured |
| GET /api/farm/satellite/hls | Coordinates | Most recent clear Landsat NDVI sample found among recent scenes, scene and observation timestamp. The Lusaka pin also has an explicitly dated archive fallback. |
| GET /api/farm/satellite/srtm | Coordinates | Static SRTM elevation and local slope around the pin |
| GET /api/farm/soil | Coordinates | SoilGrids texture, water capacity and pH for the top 60 cm |
| GET /api/farm/rain-check | Coordinates, dates | NASA POWER and CHIRPS rainfall totals for the same days and how closely they agree |

Existing API clients can still access the older climate, planting-window and file-processing endpoints through the API documentation. The farmer-facing UI uses automatic retrieval.

## How each source informs a decision

| Source | Role and limits |
|---|---|
| [NASA POWER](https://power.larc.nasa.gov/docs/services/api/temporal/daily/) | Daily history for rainfall totals, dry spells and heat context. These values represent the source grid cell and daily weather is published with a delay. |
| [GPM IMERG](https://gpm.nasa.gov/data/imerg) | Near-real-time precipitation imagery through NASA GIBS. With Earthdata configured, the daily Early Run numeral supplements the water-risk context. The grid is roughly 10 km. |
| [SMAP](https://nsidc.org/data/spl4smgp/versions/8) | Regional root-zone moisture context with an authenticated HDF5 download. The grid is roughly 9 km. |
| [Landsat HLS](https://gis.earthdata.nasa.gov/portal/rest/services/C2021957657-LPCLOUD/HLSL30_V02/ImageServer) | Samples the red and near-infrared bands and rejects poor-quality pixels using the product mask. NDVI indicates vegetation greenness around the pin at roughly 30 m. The app reports the date and leaves the value unavailable when no clear sample is returned. |
| [SRTM](https://doi.org/10.5067/MEaSUREs/SRTM/SRTMGL1.003) | Elevation from the public AWS Terrain Tiles archive and slope from the neighbouring cells. The terrain describes runoff context and is static. |

NASA Earthdata CMR finds the latest granule for the authenticated numerical IMERG and SMAP paths. The application keeps the bearer token server-side and uses bounded, time-limited downloads. The browser refreshes its open evidence view around every 15 minutes. Individual provider products follow their own publication schedules.

## Scoring inputs

| Input | Source | Fallback |
|---|---|---|
| Rainfall, temperature | NASA POWER daily series, or CHIRPS for the selected dates | Bundled Lusaka history when offline |
| Evaporation | FAO-56 Penman-Monteith from POWER radiation, humidity, wind and SRTM elevation | Hargreaves from temperature |
| Soil water capacity, pH | SoilGrids texture class lookup | Loam, 120 mm/m, and the pH the grower enters |
| Starting soil moisture | 45-day spin-up before the season, or the grower's dry/moist/wet report | 50% of capacity |
| Runoff | SRTM slope | None |
| Season-to-season risk | Same dates in up to 15 earlier years (needs at least 5 seasons) | The selected season only, shown with a warning |
| Checks | SMAP root-zone moisture and Landsat NDVI compared with the water balance | Skipped when the observation is not near the season |

`engine.py` holds the model, `soil.py` and `chirps.py` fetch the soil and rainfall inputs, and `calibrate.py` validates against recorded yields.

The comparison uses [FAO indicative crop water ranges](https://www.fao.org/4/s2022e/s2022e07.htm) and [water-sensitive crop stages](https://www.fao.org/4/t7202e/t7202e05.htm). Comparing rainfall to the range is an initial screening step. Effective rainfall, evapotranspiration, irrigation volumes and local planting calendars would improve a farm-specific water balance. User observations and market scenario values are user inputs.
