$ErrorActionPreference='Stop'
$before=docker image inspect folio-parser:1 --format '{{.Id}}'
if($LASTEXITCODE -ne 0){throw 'PARSER_BASELINE_MISSING'}
$rollback=docker image inspect folio-parser:rollback-round7-20261101 --format '{{.Id}}' 2>$null
if($LASTEXITCODE -ne 0){
    docker tag $before folio-parser:rollback-round7-20261101
    if($LASTEXITCODE -ne 0){throw 'ROLLBACK_TAG_FAILED'}
} else { Write-Output "preserved_existing_rollback_image=$rollback" }
Write-Output "before_image=$before rollback_tag=folio-parser:rollback-round7-20261101"
docker build --file ops/parser.Dockerfile --tag folio-parser:1 .
if($LASTEXITCODE -ne 0){throw 'PARSER_BUILD_FAILED'}
$after=docker image inspect folio-parser:1 --format '{{.Id}}'
Write-Output "after_image=$after"
