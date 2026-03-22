$inputFile = "C:\Users\Sean\Documents\VSCode Projects\bike lane karen\311_Service_Requests_-_Austin_Transportation_and_Public_Works_20260322.csv"
$outputFile = "C:\Users\Sean\Documents\VSCode Projects\bike lane karen\311_Service_Requests_Last_365_Days.csv"

$cutoffDate = (Get-Date).AddDays(-365)

Write-Host "Today: $((Get-Date).ToString('yyyy-MM-dd'))"
Write-Host "Cutoff date: $($cutoffDate.ToString('yyyy-MM-dd'))"

# Read CSV
$rows = Import-Csv -Path $inputFile

$kept = 0
$skipped = 0
$filteredRows = @()

foreach ($row in $rows) {
    $createdDateStr = $row."Created Date"
    
    if ([string]::IsNullOrWhiteSpace($createdDateStr)) {
        $skipped++
        continue
    }
    
    try {
        # Parse date format: "2022 Mar 29 03:48:47 PM"
        $createdDate = [datetime]::ParseExact($createdDateStr, "yyyy MMM dd hh:mm:ss tt", $null)
        
        if ($createdDate -ge $cutoffDate) {
            $filteredRows += $row
            $kept++
        } else {
            $skipped++
        }
    } catch {
        Write-Host "Error parsing date: $createdDateStr - $($_.Exception.Message)"
        $skipped++
    }
}

# Export filtered rows
$filteredRows | Export-Csv -Path $outputFile -NoTypeInformation

Write-Host ""
Write-Host "Results:"
Write-Host "  Rows kept (last 365 days): $kept"
Write-Host "  Rows skipped (older): $skipped"
Write-Host "  Output file: $outputFile"
