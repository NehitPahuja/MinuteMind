param([string]$Out)
Add-Type -AssemblyName System.Speech
New-Item -ItemType Directory -Force $Out | Out-Null
$lines = @(
  @('01', 'Microsoft David Desktop', 'Okay, let us start. We need to decide on the launch date for the mobile app, and who handles the beta testing.'),
  @('02', 'Microsoft Zira Desktop',  'I think we should launch on the first of December. The payment screen is finished and QA signed off yesterday.'),
  @('03', 'Microsoft Hazel Desktop', 'I agree. But the onboarding videos are not ready yet. I can finish them by Friday the twentieth.'),
  @('04', 'Microsoft David Desktop', 'Great. So we are decided. We launch on December first. Hazel owns the onboarding videos, due Friday.'),
  @('05', 'Microsoft Zira Desktop',  'One more thing. We agreed to drop dark mode from this release and move it to January.'),
  @('06', 'Microsoft Zira Desktop',  'I will send the beta invite to the fifty testers by Wednesday.'),
  @('07', 'Microsoft David Desktop', 'Thanks. I will book the launch review with marketing for next Tuesday.')
)
$fmt = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(16000, [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen, [System.Speech.AudioFormat.AudioChannel]::Mono)
foreach ($l in $lines) {
  $s = New-Object System.Speech.Synthesis.SpeechSynthesizer
  $s.SelectVoice($l[1])
  $s.SetOutputToWaveFile((Join-Path $Out ($l[0] + '.wav')), $fmt)
  $s.Speak($l[2])
  $s.Dispose()
}
