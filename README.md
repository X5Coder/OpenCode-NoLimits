# OpenCode-NoLimits — WarpSetup Helper

> اداة مساعدة فقط: بتجهز اتصال Cloudflare WARP على Windows وبتعرض عنوان IP العام في التيرمينال.

## الخطوات (فقط شغّل البرنامج وهو يعمل الباقي تلقائيًا)

1. حمّل `WarpSetup.exe` من صفحة [Releases](../../releases) وشغّله (دبل كليك).
2. لو WARP مش متثبت: وافق بـ `y` — البرنامج يحمّل المثبّت الرسمي من Cloudflare فقط ويفتح معالج التثبيت (اقبل UAC وكمّل المعالج واضغط ENTER).
3. البرنامج يشغّل WARP (`warp-svc` + `warp-cli connect`) بدون تغيير إعداداتك، وينتظر الاتصال الحقيقي (حتى 120 ثانية).
4. بعد الاتصال يعرض عنوان IP العام في التيرمينال + هل اتغيّر عن قبل الاتصال أم لا.
5. النافذة تفضل ظاهرة — اضغط ENTER للقفل. سجل التشخيص: `%TEMP%\warp_setup_diag.log`.

## ملفات المشروع

| الملف | الوصف |
|---|---|
| `warp_setup.py` | الكود المصدري الكامل |
| `dist/WarpSetup.exe` | النسخة المبنية (تُرفع أيضًا على Releases) |
| `icon.ico` / `icon.png` | أيقونة الأداة |
| `build_exe.bat` | سكربت البناء |
| `requirements.txt` | الاعتماديات |

## البناء من المصدر

```bat
build_exe.bat
REM او يدويًا:
python -m pip install -r requirements.txt
python -m PyInstaller --onefile --console --name WarpSetup --icon icon.ico warp_setup.py
REM الناتج: dist\WarpSetup.exe
```

## تنبيه أمانة

- البرنامج **يعرض IP قبل/بعد الاتصال بصدق** — إعادة تشغيل WARP لا تعني بالضرورة IP مختلفًا، والبرنامج لا يدّعي تغيّرًا لم يتحقق منه.
- البرنامج **مسؤول عن إعداد الاتصال والتحقق من IP فقط** — لا يعد بتجديد كوتة OpenCode ولا يفتح أي حدود؛ أي استخدام مع OpenCode هو مسؤوليتك وضمن شروط خدمتهم.
- لا يعطّل Defender ولا يتجاوز UAC — كل تثبيت بموافقتك.
