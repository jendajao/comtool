param([string]$Port="COM3",[int]$Baud=115200)
$p = New-Object System.IO.Ports.SerialPort $Port,$Baud,'None',8,'One'
$p.Handshake='None'; $p.DtrEnable=$true; $p.RtsEnable=$true
try { $p.Open() } catch { [Console]::Error.WriteLine("OPEN FAIL: $($_.Exception.Message)"); exit 2 }
[Console]::Error.WriteLine("OPENED $Port @ $Baud")
$out=[Console]::OpenStandardOutput(); $in=[Console]::OpenStandardInput()
$ib=New-Object byte[] 4096; $rb=New-Object byte[] 4096
$t=$in.ReadAsync($ib,0,4096)
while($true){
  $busy=$false
  $n=$p.BytesToRead
  if($n -gt 0){ $n=$p.Read($rb,0,[Math]::Min(4096,$n)); $out.Write($rb,0,$n); $out.Flush(); $busy=$true }
  if($t.IsCompleted){ $k=$t.Result; if($k -le 0){break}; $p.Write($ib,0,$k); $t=$in.ReadAsync($ib,0,4096); $busy=$true }
  if(-not $busy){ Start-Sleep -Milliseconds 5 }
}
$p.Close()
