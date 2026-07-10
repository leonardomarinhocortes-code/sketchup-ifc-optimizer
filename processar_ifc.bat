@echo off
REM ============================================================================
REM  LIMPADOR IFC - arrastar e soltar
REM  Arraste um arquivo .ifc (exportado do SketchUp) sobre este .bat.
REM  Ele remove o entourage, deduplica os vertices, confere a integridade e
REM  gera, ao lado do original, um unico arquivo: "<nome>_limpo.ifc" (-> Revit).
REM ============================================================================
setlocal

if "%~1"=="" (
  echo.
  echo  Como usar: arraste um arquivo .ifc sobre este processar_ifc.bat
  echo  Ou pela linha de comando:  processar_ifc.bat "C:\caminho\ARQUIVO.ifc"
  echo.
  pause
  exit /b 1
)

REM "%~dp0" = pasta deste .bat (onde esta o .py).  "%~1" = arquivo arrastado.
python "%~dp0limpar_ifc.py" "%~1"

echo.
pause
endlocal
