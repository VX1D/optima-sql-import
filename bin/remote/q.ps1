# Called by bin/sql on the Windows host: sqlcmd with the `sa` password read from a file (never from the command line).
param([string]$In, [string]$Out, [string]$Server, [string]$Db, [string]$PasswordFile, [ValidateSet('W','y0')][string]$Format = 'W')
$env:SQLCMDPASSWORD = (Get-Content $PasswordFile -Raw).Trim()
$fmt = if ($Format -eq 'y0') { @('-y', '0') } else { @('-W') }
sqlcmd -S $Server -U sa -d $Db -l 15 -t 600 @fmt -s "|" -I -f 65001 -i $In -o $Out
