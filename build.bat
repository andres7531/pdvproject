@echo off
REM Gera dist\PDV.exe (execute em um Windows com Python 3.10+ instalado)
python -m pip install -r requirements.txt pyinstaller || goto :erro
python -m PyInstaller --noconfirm --onefile --windowed --name PDV ^
  --collect-all pynfe --collect-all signxml --collect-all lxml pdv.py || goto :erro
echo.
echo Pronto: dist\PDV.exe
echo Coloque ao lado do PDV.exe: config.json e seu certificado .pfx (o pdv.db e a pasta xml\ sao criados ali).
goto :fim
:erro
echo Falha no build.
:fim
pause
