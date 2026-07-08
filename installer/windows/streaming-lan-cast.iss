; Inno Setup script for Streaming LAN Cast: the local helper.
; Per-user install (no admin). Build with:  ISCC.exe installer\windows\streaming-lan-cast.iss
; (the exe bundle in ..\..\dist\streaming-lan-cast-helper\ must already be built by PyInstaller)
; This file MUST be saved as UTF-8 with BOM (it contains non-ASCII translations).

#define MyAppName "Streaming LAN Cast"
#define MyAppVersion "0.5.1"
#define MyAppPublisher "Fabrizio Santi"
#define MyAppExe "streaming-lan-cast-helper.exe"

[Setup]
AppId={{8F3A1C72-5E94-4B0D-A6F1-2C7E9D4B8A30}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={localappdata}\Programs\{#MyAppName}
DisableProgramGroupPage=yes
DisableDirPage=yes
PrivilegesRequired=lowest
OutputDir={#SourcePath}\..\..\dist\installer
OutputBaseFilename=Streaming-LAN-Cast-Setup-{#MyAppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayName={#MyAppName}
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "en";   MessagesFile: "compiler:Default.isl"
Name: "es";   MessagesFile: "compiler:Languages\Spanish.isl"
Name: "ptbr"; MessagesFile: "compiler:Languages\BrazilianPortuguese.isl"
Name: "fr";   MessagesFile: "compiler:Languages\French.isl"
Name: "de";   MessagesFile: "compiler:Languages\German.isl"
Name: "it";   MessagesFile: "compiler:Languages\Italian.isl"
Name: "ru";   MessagesFile: "compiler:Languages\Russian.isl"
Name: "ja";   MessagesFile: "compiler:Languages\Japanese.isl"
Name: "ko";   MessagesFile: "compiler:Languages\Korean.isl"
Name: "zhcn"; MessagesFile: "{#SourcePath}\ChineseSimplified.isl"

[CustomMessages]
en.FinishMsg=Installation complete, the helper is running and starts automatically at login.
es.FinishMsg=Instalación completa, el ayudante está corriendo y arranca al iniciar sesión.
ptbr.FinishMsg=Instalação concluída, o auxiliar está em execução e inicia ao entrar.
fr.FinishMsg=Installation terminée, l'assistant est lancé et démarre à l'ouverture de session.
de.FinishMsg=Installation abgeschlossen, der Helfer läuft und startet bei der Anmeldung.
it.FinishMsg=Installazione completata, l'helper è in esecuzione e si avvia all'accesso.
ru.FinishMsg=Установка завершена, помощник запущен и стартует при входе в систему.
ja.FinishMsg=インストール完了。ヘルパーは実行中で、ログイン時に起動します。
ko.FinishMsg=설치 완료. 도우미가 실행 중이며 로그인 시 시작됩니다.
zhcn.FinishMsg=安装完成。助手正在运行，并会在登录时启动。
en.TokLabel=Paste this token into the browser extension (Options):
es.TokLabel=Pegá este token en la extensión del navegador (Opciones):
ptbr.TokLabel=Cole este token na extensão do navegador (Opções):
fr.TokLabel=Collez ce jeton dans l'extension du navigateur (Options) :
de.TokLabel=Füge diesen Token in die Browser-Erweiterung ein (Optionen):
it.TokLabel=Incolla questo token nell'estensione del browser (Opzioni):
ru.TokLabel=Вставьте этот токен в расширение браузера (Настройки):
ja.TokLabel=このトークンをブラウザ拡張機能に貼り付けてください（オプション）:
ko.TokLabel=이 토큰을 브라우저 확장 프로그램에 붙여넣으세요 (옵션):
zhcn.TokLabel=将此令牌粘贴到浏览器扩展中（选项）：
en.CopyBtn=Copy token
es.CopyBtn=Copiar token
ptbr.CopyBtn=Copiar token
fr.CopyBtn=Copier le jeton
de.CopyBtn=Token kopieren
it.CopyBtn=Copia token
ru.CopyBtn=Копировать токен
ja.CopyBtn=トークンをコピー
ko.CopyBtn=토큰 복사
zhcn.CopyBtn=复制令牌
en.Copied=Token copied. Paste it into the extension's Options.
es.Copied=Token copiado. Pegalo en las Opciones de la extensión.
ptbr.Copied=Token copiado. Cole nas Opções da extensão.
fr.Copied=Jeton copié. Collez-le dans les Options de l'extension.
de.Copied=Token kopiert. Füge ihn in die Optionen der Erweiterung ein.
it.Copied=Token copiato. Incollalo nelle Opzioni dell'estensione.
ru.Copied=Токен скопирован. Вставьте его в Настройки расширения.
ja.Copied=トークンをコピーしました。拡張機能のオプションに貼り付けてください。
ko.Copied=토큰이 복사되었습니다. 확장 프로그램 옵션에 붙여넣으세요.
zhcn.Copied=已复制令牌。请将其粘贴到扩展的"选项"中。

[Files]
Source: "{#SourcePath}\..\..\dist\streaming-lan-cast-helper\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion
Source: "{#SourcePath}\run-hidden.vbs"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\run-hidden.vbs"; Comment: "Start the Streaming LAN Cast helper"

[Registry]
; autostart the helper (hidden) at login
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; \
  ValueName: "{#MyAppName}"; ValueData: "wscript.exe ""{app}\run-hidden.vbs"""; Flags: uninsdeletevalue

[Run]
; start the helper during install so the per-install token gets generated (shown on the final page)
Filename: "wscript.exe"; Parameters: """{app}\run-hidden.vbs"""; Flags: nowait runhidden

[UninstallRun]
Filename: "taskkill.exe"; Parameters: "/f /im {#MyAppExe} /t"; Flags: runhidden; RunOnceId: "stophelper"

[UninstallDelete]
Type: filesandordirs; Name: "{%USERPROFILE}\.streaming-lan-cast"

[Code]
var
  GToken: String;

function ReadToken(): String;
var
  path: String;
  content: AnsiString;
  i: Integer;
begin
  Result := '';
  path := ExpandConstant('{%USERPROFILE}\.streaming-lan-cast\token');
  for i := 1 to 24 do  { wait up to ~7s for the helper to write the token }
  begin
    if FileExists(path) then
      if LoadStringFromFile(path, content) then
        if Trim(String(content)) <> '' then
        begin
          Result := Trim(String(content));
          Exit;
        end;
    Sleep(300);
  end;
end;

procedure CopyTokenClick(Sender: TObject);
var
  rc: Integer;
begin
  if GToken <> '' then
  begin
    Exec(ExpandConstant('{cmd}'), '/c echo ' + GToken + '|clip', '', SW_HIDE, ewWaitUntilTerminated, rc);
    MsgBox(ExpandConstant('{cm:Copied}'), mbInformation, MB_OK);
  end;
end;

procedure CurPageChanged(CurPageID: Integer);
var
  cap: TNewStaticText;
  ed: TNewEdit;
  btn: TNewButton;
begin
  if CurPageID = wpFinished then
  begin
    GToken := ReadToken();
    WizardForm.FinishedLabel.Caption := ExpandConstant('{cm:FinishMsg}');

    cap := TNewStaticText.Create(WizardForm);
    cap.Parent := WizardForm.FinishedPage;
    cap.AutoSize := True;
    cap.Left := WizardForm.FinishedLabel.Left;
    cap.Top := WizardForm.FinishedLabel.Top + ScaleY(44);
    cap.Caption := ExpandConstant('{cm:TokLabel}');

    ed := TNewEdit.Create(WizardForm);
    ed.Parent := WizardForm.FinishedPage;
    ed.Left := WizardForm.FinishedLabel.Left;
    ed.Top := cap.Top + ScaleY(20);
    ed.Width := WizardForm.FinishedLabel.Width;
    ed.ReadOnly := True;
    ed.Font.Style := [fsBold];
    if GToken <> '' then
      ed.Text := GToken
    else
      ed.Text := '(see the extension Options)';

    btn := TNewButton.Create(WizardForm);
    btn.Parent := WizardForm.FinishedPage;
    btn.Left := WizardForm.FinishedLabel.Left;
    btn.Top := ed.Top + ScaleY(32);
    btn.Width := ScaleX(140);
    btn.Height := ScaleY(26);
    btn.Caption := ExpandConstant('{cm:CopyBtn}');
    btn.OnClick := @CopyTokenClick;
    btn.Enabled := GToken <> '';
  end;
end;
