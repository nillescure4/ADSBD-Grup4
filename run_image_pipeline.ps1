$steps = @(
    "src.ingestion.images.images_to_landing",
    "src.ingestion.images.images_temporal_to_persistent",
    "src.formatting.images.persistent_to_formatted",
    "src.trusted.images.build_trusted_images",
    "src.trusted.images.quality_report"
)

foreach ($module in $steps) {
    Write-Host "==> $module"
    python -m $module
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Failed: $module (exit code $LASTEXITCODE)"
        exit $LASTEXITCODE
    }
}
Write-Host "Pipeline finished."