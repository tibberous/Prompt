Unicode true
!include "MUI2.nsh"
!include "FileFunc.nsh"
!include "LogicLib.nsh"

Name "Prompt"
OutFile "/dist/PromptSetup.exe"
InstallDir "$LOCALAPPDATA\Programs\Prompt"
InstallDirRegKey HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\Prompt" "InstallLocation"
RequestExecutionLevel user

VIProductVersion "1.0.0.0"
VIAddVersionKey "ProductName" "Prompt"
VIAddVersionKey "CompanyName" "AcquisitionInvest LLC"
VIAddVersionKey "LegalCopyright" "AcquisitionInvest LLC © 2026"
VIAddVersionKey "FileDescription" "Prompt 1.0.0 - Desktop Prompt Workbench"
VIAddVersionKey "FileVersion" "1.0.0.0"
VIAddVersionKey "ProductVersion" "1.0.0.0"
VIAddVersionKey "Comments" "Prompt 1.0.0 | Author: Trenton Tompkins | Coded by ChatGPT | http://www.trentontompkins.com | TrentTompkins@gmail.com | (724) 431-4207"
Icon "/dist/icon.ico"
UninstallIcon "/dist/icon.ico"
!define MUI_ABORTWARNING
!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "English"

Section "Install"
  ; Prompt is installed per-user into LocalAppData so shipped MD/HTML/workflow files remain editable.
  ; Never install editable Prompt content under Program Files ACLs.
  SetOutPath "$INSTDIR"
  ; Application files are added after building with --PyInstaller or --Nikita.
  ExecWait '"$SYSDIR\attrib.exe" -R "$INSTDIR\*.*" /S /D'
  ExecWait '"$SYSDIR\attrib.exe" -R "$INSTDIR" /D'
  WriteUninstaller "$INSTDIR\Uninstall.exe"
  SetShellVarContext current
  CreateDirectory "$SMPROGRAMS\Prompt"
  CreateShortcut "$SMPROGRAMS\Prompt\Prompt.lnk" "$INSTDIR\Prompt.exe"
  CreateShortcut "$SMPROGRAMS\Prompt\Uninstall.lnk" "$INSTDIR\Uninstall.exe"
  CreateShortcut "$DESKTOP\Prompt.lnk" "$INSTDIR\Prompt.exe"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\Prompt" "DisplayName" "Prompt"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\Prompt" "DisplayVersion" "1.0.0.0"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\Prompt" "Publisher" "AcquisitionInvest LLC"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\Prompt" "URLInfoAbout" "http://www.trentontompkins.com"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\Prompt" "Contact" "TrentTompkins@gmail.com"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\Prompt" "HelpTelephone" "(724) 431-4207"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\Prompt" "InstallLocation" "$INSTDIR"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\Prompt" "UninstallString" "$\"$INSTDIR\Uninstall.exe$\""
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\Prompt" "QuietUninstallString" "$\"$INSTDIR\Uninstall.exe$\" /S"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\Prompt" "DisplayIcon" "$INSTDIR\Prompt.exe"
  WriteRegDWORD HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\Prompt" "NoModify" 1
  WriteRegDWORD HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\Prompt" "NoRepair" 1
  ${GetSize} "$INSTDIR" "/S=0K" $0 $1 $2
  IntFmt $0 "0x%08X" $0
  WriteRegDWORD HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\Prompt" "EstimatedSize" "$0"
SectionEnd

Section "Uninstall"
  SetShellVarContext current
  Delete "$DESKTOP\Prompt.lnk"
  RMDir /r "$SMPROGRAMS\Prompt"
  ExecWait '"$SYSDIR\attrib.exe" -R "$INSTDIR\*.*" /S /D'
  ExecWait '"$SYSDIR\attrib.exe" -R "$INSTDIR" /D'
  ${If} "$INSTDIR" != ""
    RMDir /r "$INSTDIR"
  ${EndIf}
  DeleteRegKey HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\Prompt"
SectionEnd
