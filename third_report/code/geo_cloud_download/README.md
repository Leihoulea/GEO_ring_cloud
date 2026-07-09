# GEO Cloud Download

Run with the `pytorch` conda environment:

```powershell
conda run -n pytorch python code\geo_cloud_download\geo_cloud_downloader.py --root E:\GEO_Cloud_2024 inventory
conda run -n pytorch python code\geo_cloud_download\geo_cloud_downloader.py --root E:\GEO_Cloud_2024 download-test-day --date 2024-03-12
```

EUMETSAT credentials must be provided only as process environment variables:

```powershell
$env:EUMETSAT_CONSUMER_KEY = "<consumer key>"
$env:EUMETSAT_CONSUMER_SECRET = "<consumer secret>"
conda run -n pytorch python code\geo_cloud_download\geo_cloud_downloader.py --root E:\GEO_Cloud_2024 first-round
Remove-Item Env:\EUMETSAT_CONSUMER_KEY
Remove-Item Env:\EUMETSAT_CONSUMER_SECRET
```

Outputs are written under `E:\GEO_Cloud_2024\manifests`, `logs`, and the platform/product/day/hour data folders.

## Meteosat API smoke test

The Meteosat path uses EUMETSAT Data Store credentials and does not persist
secrets. In a PowerShell session:

```powershell
$env:EUMETSAT_CONSUMER_KEY = "<consumer key>"
$env:EUMETSAT_CONSUMER_SECRET = "<consumer secret>"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File code\geo_cloud_download\run_meteosat_api_smoke.ps1
Remove-Item Env:\EUMETSAT_CONSUMER_KEY
Remove-Item Env:\EUMETSAT_CONSUMER_SECRET
```

The smoke test writes:

- `E:\GEO_Cloud_2024\manifests\meteosat_collection_options.json`
- `E:\GEO_Cloud_2024\manifests\meteosat_smoke_2024-03-12_0000.json`
