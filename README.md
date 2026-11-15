# MundaLens

MundaLens helps a grower compare the current crop with two alternative rotations. Select the farm on a satellite map, use your device location with permission, or enter coordinates. The app retrieves NASA data for the pin automatically. Lusaka is an optional example and the map starts without a selected farm.

## Run on Fedora

Extract the ZIP, open the `MundaLens` folder in VS Code, and run these commands in its terminal.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m uvicorn app:app --host 127.0.0.1 --port 8000
```

Open [MundaLens](http://127.0.0.1:8000). Select **Try Lusaka** for a verified historical example, or search for a place or select a farm on the map. Use **Last rainy season** under Season for a sensible date range. The Lusaka example from 1 November 2024 through 30 April 2025 returned 181 NASA POWER records, 614.8 mm of rain and a 21-day dry spell below 1 mm/day. Look under **Latest NASA evidence** for the dated IMERG rainfall map. Landsat NDVI is shown when a clear observation is available. The Lusaka example also has a dated Landsat 9 archive as a fallback. Change the crops, water access or dates and select **Compare options** again. Open [API documentation](http://127.0.0.1:8000/docs) while the server is running.

## Earthdata setup

The public IMERG map, NASA POWER history and open map imagery work without an account. Numerical IMERG daily rainfall and SMAP root-zone moisture use restricted download endpoints.

1. [Create a free NASA Earthdata Login account](https://urs.earthdata.nasa.gov/users/new).
2. Sign in and follow the [token instructions](https://urs.earthdata.nasa.gov/documentation/for_users/user_token). If prompted for application approval by the relevant NASA data centers, follow their [authorization instructions](https://urs.earthdata.nasa.gov/documentation/for_users/data_access/curl_and_wget). Tokens are valid for 60 days.
3. Stop the server with Ctrl+C. Run these commands in the same terminal and paste the token when prompted. Input is hidden.

```bash
read -rsp 'NASA Earthdata token: ' NASA_EARTHDATA_TOKEN
printf '\n'
export NASA_EARTHDATA_TOKEN
python -m uvicorn app:app --host 127.0.0.1 --port 8000
```

The token stays on your server. Farmers select a location and the app handles retrieval. The app was tested without a token against the public data sources and Earthdata catalogs. Raw authorized downloads still need testing with a valid token. Landsat catalog query and cloud masking were checked at the Lusaka pin, where the recent samples were excluded by quality flags. The bundled 25 March 2025 scene yielded 439 valid pixels around that pin.

## How the ranking works

Each crop is scored with a daily water balance (FAO-56). Reference evaporation uses Penman-Monteith with NASA POWER solar radiation, humidity and wind, and falls back to temperature-only Hargreaves if those are missing. Soil texture, water capacity and pH come from [SoilGrids](https://soilgrids.org) at the pin, slope from SRTM reduces how much rain soaks in, and soil moisture at the start of the season is spun up from the 45 days of weather before it unless you report the soil as dry, moist or wet. The same dates are then replayed for up to 15 earlier years, and crops are ranked on the typical (median) and bad-year result, so one unusual season does not decide the answer. SMAP and Landsat are used as consistency checks and are never blended into the score.

Rainfall can be cross-checked against [CHIRPS](https://www.chc.ucsb.edu/data/chirps) (Africa only). Under **More options**, choose CHIRPS to use it for the selected dates. SoilGrids and CHIRPS were written against their public documentation and tested with mocked responses. Run the app once with internet access and confirm the Soil and Rainfall cross-check cards show values.

### Checking against real yields

`calibrate.py` compares modelled and observed yields. Put your records in `data/yield_observations.csv` (columns `lat, lon, season_start, season_end, crop, yield_t_ha`) and run `python calibrate.py data/yield_observations.csv`. It reports how well the model orders good and bad seasons for each crop and never changes parameters. If you decide to adjust a crop, add `data/calibration.json`, for example `{"ky_scale": {"Maize": 0.9}}`. The app then shows a "Locally calibrated" label.

### Tests

```bash
python -m pip install -r requirements-dev.txt
python -m pytest tests
```

## Satellite map

The default imagery map uses Esri. To enable the optional Google Satellite basemap, configure a browser-restricted [Google Maps JavaScript API key](https://developers.google.com/maps/documentation/javascript/get-api-key) as `GOOGLE_MAPS_BROWSER_KEY` before starting the server. Google Satellite was not tested with an API key. Browser location requires permission and a secure context. Localhost is suitable for the local demo. Use HTTPS when hosting it for others.

## What the comparison means

NASA rainfall, soil moisture, vegetation and elevation are observations or estimates at different scales. The comparison uses FAO crop water requirements and drought sensitivity as a first screening step. The grower selects crops suitable for the region and can add local soil and crop information. Confirm local varieties, planting windows, drainage and market conditions before making a planting decision.

## Soil and error logging

- Soil comes from the bundled SoilGrids maps first (Lusaka, instant), then ISRIC's map service, then the REST service, which ISRIC has paused. A failure prints the reason in the server terminal.
- Failed NASA evidence downloads also print the real error in the terminal.
- Start with `./run.sh`. It loads `.env` (a line `export NASA_EARTHDATA_TOKEN=...`, permissions 600) when present. Never commit or share `.env`.
