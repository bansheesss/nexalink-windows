# راه‌اندازی سرور Relay ویونا‌لینک

این راهنما برای Ubuntu Server 24.04 LTS و Python 3.12 نوشته شده است. Relay فقط واسطه‌ی اتصال است؛ میزبان Windows و Viewer (از جمله Android) هر دو اتصال خروجی TCP به آن برقرار می‌کنند. پس نیازی به IP عمومی یا باز کردن پورت روی کامپیوتر میزبان نیست.

## معماری و پیش‌نیازها

- یک VPS با Ubuntu Server 24.04، دسترسی sudo و IP عمومی
- یک نام دامنه یا زیردامنه مانند `relay.example.com` که رکورد DNS نوع A آن به IP سرور اشاره کند
- دسترسی خروجی TCP از کامپیوتر و گوشی به VPS
- پورت TCP شماره `7072` برای Relay و پورت `80` هنگام صدور/تمدید گواهی Let's Encrypt
- در این راهنما TLS اجباری در نظر گرفته شده است. از توکن طولانی و تصادفی استفاده می‌کنیم.

Relay فایل‌ها، تصویر و ورودی‌ها را رمزگشایی نمی‌کند؛ پس از جفت‌شدن دو سوکت، داده‌ی نشست رمز‌شده‌ی سرتاسری عبور داده می‌شود. با این حال Relay می‌تواند شناسه‌ی دستگاه‌ها، IP طرفین، زمان و حجم تقریبی اتصال را ببیند. TLS همچنین ارتباط کنترل و توکن مشترک را در مسیر تا سرور محافظت می‌کند.

## ۱. آماده‌سازی DNS و VPS

در پنل DNS دامنه، یک رکورد بسازید:

- Type: `A`
- Name/Host: `relay`
- Value: IP عمومی VPS
- مثال: `relay.example.com`

اگر IPv6 سرور را درست پیکربندی نکرده‌اید، رکورد AAAA نسازید.

به VPS با SSH وصل شوید و بسته‌ها را نصب کنید:

```bash
sudo apt update
sudo apt install -y git python3 python3-venv python3-pip certbot ufw
```

پورت SSH را قبل از فعال‌کردن UFW مجاز کنید (در صورت استفاده از پورت SSH غیرپیش‌فرض، همان پورت را وارد کنید):

```bash
sudo ufw allow OpenSSH
sudo ufw allow 80/tcp
sudo ufw allow 7072/tcp
sudo ufw enable
sudo ufw status
```

اگر VPS پنل Firewall/Security Group جداگانه دارد، TCPهای 80 و 7072 را در آنجا هم مجاز کنید. پورت 7070 میزبان را روی اینترنت باز نکنید؛ کلاینت‌ها از Relay استفاده می‌کنند.

## ۲. دریافت کد و نصب محیط Python

یک کاربر سرویس بدون امکان ورود تعاملی بسازید و کد مرجع Windows را روی VPS دریافت کنید:

```bash
sudo useradd --system --no-create-home --shell /usr/sbin/nologin viunalink
sudo mkdir -p /opt/viunalink /var/lib/viunalink
sudo chown -R viunalink:viunalink /opt/viunalink /var/lib/viunalink
sudo -u viunalink git clone https://github.com/bansheesss/nexalink-windows.git /opt/viunalink
sudo -u viunalink python3 -m venv /opt/viunalink/.venv
sudo -u viunalink /opt/viunalink/.venv/bin/pip install --upgrade pip
sudo -u viunalink /opt/viunalink/.venv/bin/pip install cryptography==43.0.3
```

برای اجرای Relay فقط کتابخانه `cryptography` لازم است؛ لازم نیست رابط گرافیکی PyQt یا وابستگی‌های Capture را روی VPS نصب کنید.

## ۳. ساخت توکن مشترک

توکن را روی VPS تولید کنید:

```bash
python3 -c 'import secrets; print(secrets.token_urlsafe(48))'
```

خروجی را در Password Manager نگه دارید. آن را در چت عمومی، GitHub، اسکرین‌شات یا فایل داخل مخزن قرار ندهید. همین توکن باید در تنظیمات Relay میزبان Windows و Viewer اندروید وارد شود.

فایل محیط سرویس را بسازید:

```bash
sudo install -d -m 0750 /etc/viunalink
sudo nano /etc/viunalink/relay.env
```

محتوا (به‌جای مقدار نمونه، توکن واقعی را بگذارید):

```ini
RELAY_TOKEN=PASTE_YOUR_LONG_RANDOM_TOKEN_HERE
```

سپس دسترسی فایل را محدود کنید:

```bash
sudo chown root:root /etc/viunalink/relay.env
sudo chmod 600 /etc/viunalink/relay.env
```

## ۴. دریافت گواهی TLS

مطمئن شوید DNS دامنه به VPS اشاره می‌کند و پورت TCP 80 از اینترنت قابل دسترسی است. سپس:

```bash
sudo certbot certonly --standalone -d relay.example.com
sudo install -d -o root -g viunalink -m 0750 /etc/viunalink/tls
sudo install -o root -g viunalink -m 0640 \
  /etc/letsencrypt/live/relay.example.com/fullchain.pem \
  /etc/viunalink/tls/fullchain.pem
sudo install -o root -g viunalink -m 0640 \
  /etc/letsencrypt/live/relay.example.com/privkey.pem \
  /etc/viunalink/tls/privkey.pem
```

نام دامنه را در همه‌ی مسیرهای بالا با دامنه‌ی واقعی خود عوض کنید.

برای کپی خودکار گواهی پس از تمدید، hook زیر را ایجاد کنید:

```bash
sudo tee /etc/letsencrypt/renewal-hooks/deploy/viunalink-relay.sh >/dev/null <<'EOF'
#!/bin/sh
set -eu
install -d -o root -g viunalink -m 0750 /etc/viunalink/tls
install -o root -g viunalink -m 0640 /etc/letsencrypt/live/relay.example.com/fullchain.pem /etc/viunalink/tls/fullchain.pem
install -o root -g viunalink -m 0640 /etc/letsencrypt/live/relay.example.com/privkey.pem /etc/viunalink/tls/privkey.pem
systemctl restart viunalink-relay
EOF
sudo chmod 750 /etc/letsencrypt/renewal-hooks/deploy/viunalink-relay.sh
```

در hook هم `relay.example.com` را با دامنه‌ی واقعی جایگزین کنید. برای بررسی تمدید آزمایشی:

```bash
sudo certbot renew --dry-run
```

## ۵. نصب و فعال‌کردن سرویس systemd

فایل سرویس موجود در `deploy/viunalink-relay.service` را نصب کنید:

```bash
sudo cp /opt/viunalink/deploy/viunalink-relay.service /etc/systemd/system/viunalink-relay.service
sudo systemctl daemon-reload
sudo systemctl enable --now viunalink-relay
sudo systemctl status viunalink-relay --no-pager
```

مشاهده‌ی لاگ زنده:

```bash
sudo journalctl -u viunalink-relay -f
```

سرویس باید روی `0.0.0.0:7072` گوش کند. اگر شروع نشد، اول این موارد را بررسی کنید: مسیر گواهی‌ها، مالکیت و مجوز فایل‌ها، توکن در فایل env، نصب cryptography و بازبودن TCP/7072.

## ۶. تنظیم میزبان ViunaLink در Windows

روی کامپیوتری که قرار است کنترل شود:

1. ViunaLink Windows را باز کنید.
2. وارد «تنظیمات» شوید. اگر اولین بار است، رمز محافظ تنظیمات را بسازید.
3. زبانه‌ی «شبکه و اینترنت» را باز کنید.
4. در «سرور ریلی» فقط نام دامنه را وارد کنید: `relay.example.com`؛ ننویسید `https://` و مسیر URL وارد نکنید.
5. پورت را `7072` بگذارید.
6. همان توکن ساخته‌شده روی VPS را در «توکن ریلی» وارد کنید.
7. گزینه‌ی «اتصال امن TLS به ریلی» را روشن کنید.
8. تنظیمات را ذخیره کنید و اشتراک‌گذاری/Host را شروع کنید.

میزبان یک اتصال خروجی پایدار به Relay باز می‌کند و هر ۱۵ ثانیه پیام سلامت می‌فرستد. در وضعیت Host باید Relay آنلاین شود. اگر تنظیمات را وقتی Host فعال است عوض کردید، Host را یک بار متوقف و دوباره شروع کنید.

## ۷. اتصال از Android یا دستگاه دیگر

در اپ ViunaLink Android:

1. بخش «اتصال به کامپیوتر» را باز کنید.
2. در «آدرس IP یا شناسه‌ی ۹ رقمی»، **شناسه‌ی ۹ رقمی میزبان Windows** را وارد کنید. برای Relay از IP عمومی یا IP محلی میزبان استفاده نکنید.
3. رمز میزبان را وارد کنید (رمز دائمی یا رمز یک‌بارمصرف معتبر).
4. «تنظیم اتصال اینترنتی (Relay)» را باز کنید.
5. آدرس سرور را `relay.example.com`، پورت را `7072` و همان توکن را وارد کنید.
6. گزینه‌ی «استفاده از TLS» را روشن کنید و «اتصال» را بزنید.

Android ابتدا همان شناسه را در LAN جست‌وجو می‌کند؛ اگر میزبان در شبکه محلی پیدا نشود، از Relay استفاده می‌کند. به همین دلیل برای اتصال اینترنتی باید شناسه‌ی ۹ رقمی وارد شود؛ واردکردن IP مسیر اتصال مستقیم را انتخاب می‌کند. اندروید Viewer است و خودش Host ثبت‌شونده در Relay نیست.

## ۸. عیب‌یابی

- **Relay در Windows آفلاین است:** `sudo systemctl status viunalink-relay` و `journalctl -u viunalink-relay -n 100 --no-pager` را ببینید؛ DNS، فایروال خروجی و گواهی TLS را بررسی کنید.
- **bad_token:** توکن در VPS، Windows و Android باید دقیقاً یکسان باشد؛ فاصله‌ی ابتدا/انتها نداشته باشد.
- **offline:** میزبان Windows هنوز به Relay ثبت نشده، Host اجرا نیست یا شناسه اشتباه است.
- **id_taken:** آن شناسه قبلاً با کلید دستگاه دیگری ثبت شده است. میزبان Windows طبق منطق برنامه شناسه‌ی جدید می‌گیرد؛ شناسه‌ی جدید را در Android وارد کنید.
- **TLS/certificate error:** نام دامنه باید با نام داخل گواهی یکی باشد؛ در فیلد Server فقط hostname را بنویسید و TLS را روشن کنید.
- **اتصال LAN کار می‌کند ولی اینترنت نه:** در VPS پورت TCP 7072 را در UFW و Firewall پنل ارائه‌دهنده باز کنید؛ در مسیر Relay از شناسه‌ی ۹ رقمی استفاده کنید.
- **بعد از راه‌اندازی مجدد VPS شناسه‌ها از بین رفته‌اند:** فایل `/var/lib/viunalink/registry.json` را حذف نکنید. این فایل مالکیت کلید عمومی شناسه‌ها را نگه می‌دارد؛ از آن نسخه‌ی پشتیبان بگیرید.

## ۹. نگهداری و امنیت

- توکن قوی و یکتا انتخاب کنید و فقط در سه محل موردنیاز وارد کنید.
- پورت `7072/tcp` را عمومی کنید؛ پورت مدیریت SSH را فقط برای IPهای مورداعتماد محدود کنید.
- سیستم‌عامل و بسته‌های VPS را مرتب به‌روزرسانی کنید.
- از `/var/lib/viunalink/registry.json` نسخه‌ی پشتیبان بگیرید.
- اگر توکن افشا شد، توکن جدید بسازید، فایل `relay.env` را تغییر دهید، سرویس را restart کنید و مقدار جدید را در همه‌ی کلاینت‌ها جایگزین کنید.
- Relay به‌تنهایی احراز هویت میزبان را جایگزین نمی‌کند؛ همچنان رمز ViunaLink را امن نگه دارید و در صورت نیاز تأیید اتصال میزبان را فعال کنید.
