$url = 'http://127.0.0.1:5000'
$health = "$url/health"
for ($attempt = 0; $attempt -lt 45; $attempt++) {
    try {
        $response = Invoke-WebRequest -Uri $health -TimeoutSec 2 -UseBasicParsing
        if ($response.StatusCode -eq 200) {
            Start-Process $url
            exit 0
        }
    } catch {
        Start-Sleep -Seconds 1
    }
}
Write-Output 'DukaanFlow AI did not become ready. Check the server window for the startup error.'
