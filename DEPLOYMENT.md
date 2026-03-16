# Deployment Guide

## Prerequisites

- Python 3.8 or higher
- pip package manager
- Virtual environment (recommended)

## Installation Steps

### 1. Clone/Download the Project
```bash
cd a:\project
```

### 2. Create Virtual Environment
```bash
python -m venv venv

# Windows
venv\Scripts\activate

# Linux/Mac
source venv/bin/activate
```

### 3. Install Dependencies
```bash
pip install -r requirements.txt
```

**Note:** Installing `face-recognition` may require:
- CMake
- dlib (may need Visual C++ Build Tools on Windows)
- See: https://github.com/ageitgey/face_recognition#installation

### 4. Configure Environment
```bash
# Copy example env file
cp .env.example .env

# Edit .env with your settings
# IMPORTANT: Change SECRET_KEY, JWT_SECRET_KEY, and ENCRYPTION_KEY in production!
```

Render note (ArcFace model):
- The app now supports auto-downloading `arcface_r100.onnx` if missing.
- Set:
  - `EMBEDDING_MODEL_PATH=models/arcface_r100.onnx`
  - `EMBEDDING_MODEL_URL=<public direct download URL to arcface_r100.onnx>`
- On startup, if the file is not present in `models/`, it will be downloaded automatically.

### 5. Initialize Database
```bash
python init_db.py
```

Or let the app create it automatically on first run.

### 6. Run the Application

**Development:**
```bash
python app.py
```

**Production (using Gunicorn):**
```bash
pip install gunicorn
gunicorn -w 4 -b 0.0.0.0:5000 app:create_app()
```

## Production Deployment

### Security Checklist

1. **Change Default Credentials**
   - Default admin: `admin` / `admin123`
   - Change immediately after first login

2. **Environment Variables**
   - Set strong `SECRET_KEY` (random 32+ character string)
   - Set strong `JWT_SECRET_KEY` (different from SECRET_KEY)
   - Set strong `ENCRYPTION_KEY` (exactly 32 bytes)
   - Set `FLASK_ENV=production`

3. **Database**
   - Use PostgreSQL or MySQL instead of SQLite for production
   - Update `DATABASE_URL` in `.env`
   - Example: `postgresql://user:password@localhost/attendance_db`
   - Supabase pooler example:
     `postgresql://postgres.<PROJECT_REF>:<DB_PASSWORD>@aws-1-eu-west-3.pooler.supabase.com:6543/postgres?sslmode=require`

4. **HTTPS**
   - Use reverse proxy (Nginx/Apache) with SSL certificate
   - Never expose API without HTTPS in production

5. **File Permissions**
   - Restrict access to `.env` file
   - Secure `face_data/` and `uploads/` directories

### Nginx Configuration Example

```nginx
server {
    listen 80;
    server_name your-domain.com;
    
    location / {
        proxy_pass http://127.0.0.1:5000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

### Systemd Service Example

Create `/etc/systemd/system/attendance-api.service`:

```ini
[Unit]
Description=Facial Recognition Attendance API
After=network.target

[Service]
User=www-data
WorkingDirectory=/path/to/project
Environment="PATH=/path/to/venv/bin"
ExecStart=/path/to/venv/bin/gunicorn -w 4 -b 127.0.0.1:5000 app:create_app()
Restart=always

[Install]
WantedBy=multi-user.target
```

Enable and start:
```bash
sudo systemctl enable attendance-api
sudo systemctl start attendance-api
```

## Hardware Recommendations

### Minimum Requirements
- CPU: 2+ cores
- RAM: 4GB
- Storage: 10GB+ (for face data and database)

### Recommended for Production
- CPU: 4+ cores (face recognition is CPU-intensive)
- RAM: 8GB+
- Storage: 50GB+ SSD
- GPU: Optional but recommended for faster face recognition

### Camera/Image Capture
- Webcam or IP camera for live capture
- Minimum resolution: 640x480
- Recommended: 1280x720 or higher
- Good lighting conditions improve accuracy

## Monitoring and Maintenance

### Logs
- Application logs: `logs/app_YYYYMMDD.log`
- Monitor for errors and performance issues

### Database Backup
```bash
# SQLite
cp attendance_system.db attendance_system_backup_$(date +%Y%m%d).db

# PostgreSQL
pg_dump attendance_db > backup_$(date +%Y%m%d).sql
```

### Performance Tuning

1. **Face Recognition Tolerance**
   - Lower tolerance (0.4-0.5) = stricter matching
   - Higher tolerance (0.6-0.7) = more lenient matching
   - Adjust in `.env`: `FACE_ENCODING_TOLERANCE=0.6`

2. **Database Indexing**
   - Already indexed on: `student_id`, `email`, `session_id`, `timestamp`
   - Monitor query performance

3. **Caching**
   - Consider Redis for session management
   - Cache frequently accessed student data

## Troubleshooting

### Face Recognition Not Working
- Ensure dlib and face-recognition are properly installed
- Check image quality and lighting
- Verify face is clearly visible in images

### Database Errors
- Check database file permissions
- Verify DATABASE_URL in .env
- Run `python init_db.py` to recreate tables
- Check `/health/db` to confirm DB connectivity

### Import Errors
- Ensure virtual environment is activated
- Reinstall dependencies: `pip install -r requirements.txt`
- Check Python version: `python --version` (need 3.8+)

## Support

For issues or questions, check:
1. Application logs in `logs/` directory
2. API documentation in `API_USAGE.md`
3. Error messages in API responses
