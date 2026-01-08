# Script para habilitar WSL 2 manualmente
# Ejecutar como Administrador

Write-Host "Habilitando caracteristicas de Windows..." -ForegroundColor Green

# Paso 1: Habilitar WSL
dism.exe /online /enable-feature /featurename:Microsoft-Windows-Subsystem-Linux /all /norestart

# Paso 2: Habilitar Plataforma de Maquina Virtual
dism.exe /online /enable-feature /featurename:VirtualMachinePlatform /all /norestart

Write-Host "`nCaracteristicas habilitadas. DEBES REINICIAR TU PC AHORA." -ForegroundColor Yellow
Write-Host "Despues del reinicio, ejecuta: wsl --set-default-version 2" -ForegroundColor Cyan

Read-Host "`nPresiona Enter para cerrar"
