import os
import base64
from io import BytesIO
from datetime import datetime
from functools import wraps

from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, abort
from flask_sqlalchemy import SQLAlchemy
from flask_login import (
    LoginManager, UserMixin, login_user, logout_user,
    login_required, current_user
)
from werkzeug.security import generate_password_hash, check_password_hash
from PIL import Image

# ---------------------------------------------------------
# APP CONFIG
# ---------------------------------------------------------
app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'change-this-secret-key')

# Supabase / Postgres connection string (from environment variable)
database_url = os.environ.get('DATABASE_URL', 'sqlite:///local_dev.db')
if database_url.startswith('postgres://'):
    database_url = database_url.replace('postgres://', 'postgresql://', 1)
app.config['SQLALCHEMY_DATABASE_URI'] = database_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'

# Default admin credentials (used only to auto-create first admin account)
DEFAULT_ADMIN_EMAIL = os.environ.get('ADMIN_EMAIL', 'admin@example.com')
DEFAULT_ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', 'admin123')

MAX_IMAGE_DIMENSION = 900  # px, images resized before saving as base64


# ---------------------------------------------------------
# MODELS
# ---------------------------------------------------------
class User(UserMixin, db.Model):
    __tablename__ = 'users'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(150), nullable=False)
    email = db.Column(db.String(150), unique=True, nullable=False)
    phone = db.Column(db.String(30))
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), nullable=False, default='student')  # 'student' or 'admin'
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)


class Scholarship(db.Model):
    __tablename__ = 'scholarships'
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    country = db.Column(db.String(100))
    description = db.Column(db.Text)
    deadline = db.Column(db.String(100))  # kept as text for flexibility (e.g. "Open all year")
    image_base64 = db.Column(db.Text)  # stores full data-URI string
    size_option = db.Column(db.String(20), default='medium')  # 'small' | 'medium' | 'large'
    display_order = db.Column(db.Integer, default=0)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class PageContent(db.Model):
    __tablename__ = 'page_content'
    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(100), unique=True, nullable=False)
    value = db.Column(db.Text)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class Message(db.Model):
    __tablename__ = 'messages'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)  # the student this thread belongs to
    sender = db.Column(db.String(20), nullable=False)  # 'user' or 'admin'
    text = db.Column(db.Text, nullable=False)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)
    is_read = db.Column(db.Boolean, default=False)

    user = db.relationship('User', foreign_keys=[user_id])


@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))


# ---------------------------------------------------------
# HELPERS
# ---------------------------------------------------------
def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user.is_authenticated or current_user.role != 'admin':
            abort(403)
        return f(*args, **kwargs)
    return decorated


def resize_and_encode_image(file_storage):
    """Resize an uploaded image and return it as a base64 data-URI string."""
    if not file_storage or file_storage.filename == '':
        return None
    img = Image.open(file_storage)
    img = img.convert('RGB')
    img.thumbnail((MAX_IMAGE_DIMENSION, MAX_IMAGE_DIMENSION))
    buffer = BytesIO()
    img.save(buffer, format='JPEG', quality=80)
    encoded = base64.b64encode(buffer.getvalue()).decode('utf-8')
    return f"data:image/jpeg;base64,{encoded}"


def get_page_content(key, default=''):
    entry = PageContent.query.filter_by(key=key).first()
    return entry.value if entry else default


# ---------------------------------------------------------
# PUBLIC ROUTES (no login required)
# ---------------------------------------------------------
@app.route('/')
def home():
    content = {
        'heading': get_page_content('home_heading', 'Apni Scholarship Ka Safar Aasan Banayein'),
        'subtext': get_page_content('home_subtext', 'Kam kharche mein full guidance aur application assistance.'),
    }
    featured = Scholarship.query.filter_by(is_active=True).order_by(Scholarship.display_order).limit(3).all()
    return render_template('public/home.html', content=content, featured=featured)


@app.route('/scholarships')
def scholarships_list():
    items = Scholarship.query.filter_by(is_active=True).order_by(Scholarship.display_order).all()
    return render_template('public/scholarships.html', scholarships=items)


@app.route('/scholarships/<int:scholarship_id>')
def scholarship_detail(scholarship_id):
    item = Scholarship.query.get_or_404(scholarship_id)
    return render_template('public/scholarship_detail.html', scholarship=item)


@app.route('/pricing')
def pricing():
    content = {'pricing_text': get_page_content('pricing_text', 'Sirf 2000-3000 PKR mein full assistance.')}
    return render_template('public/pricing.html', content=content)


@app.route('/about')
def about():
    content = {'about_text': get_page_content('about_text', 'Hum students ki scholarship application mein madad karte hain.')}
    return render_template('public/about.html', content=content)


# ---------------------------------------------------------
# STUDENT AUTH ROUTES
# ---------------------------------------------------------
@app.route('/signup', methods=['GET', 'POST'])
def signup():
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        email = request.form.get('email', '').strip().lower()
        phone = request.form.get('phone', '').strip()
        password = request.form.get('password', '')

        if not name or not email or not password:
            flash('Sab fields fill karein.', 'error')
            return redirect(url_for('signup'))

        if User.query.filter_by(email=email).first():
            flash('Ye email pehle se registered hai.', 'error')
            return redirect(url_for('signup'))

        new_user = User(name=name, email=email, phone=phone, role='student')
        new_user.set_password(password)
        db.session.add(new_user)
        db.session.commit()

        login_user(new_user)
        return redirect(url_for('student_chat'))

    return render_template('auth/signup.html')


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')

        user = User.query.filter_by(email=email, role='student').first()
        if user and user.check_password(password):
            login_user(user)
            next_page = request.args.get('next')
            return redirect(next_page or url_for('student_chat'))

        flash('Email ya password ghalat hai.', 'error')
        return redirect(url_for('login'))

    return render_template('auth/login.html')


@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('home'))


# ---------------------------------------------------------
# STUDENT CHAT ROUTES (login required)
# ---------------------------------------------------------
@app.route('/chat')
@login_required
def student_chat():
    if current_user.role != 'student':
        abort(403)
    return render_template('chat/student_chat.html')


@app.route('/chat/send', methods=['POST'])
@login_required
def chat_send():
    if current_user.role != 'student':
        abort(403)
    text = request.form.get('text', '').strip()
    if text:
        msg = Message(user_id=current_user.id, sender='user', text=text)
        db.session.add(msg)
        db.session.commit()
    return jsonify({'status': 'ok'})


@app.route('/chat/messages')
@login_required
def chat_messages():
    if current_user.role != 'student':
        abort(403)
    msgs = Message.query.filter_by(user_id=current_user.id).order_by(Message.timestamp).all()
    return jsonify([
        {'sender': m.sender, 'text': m.text, 'timestamp': m.timestamp.strftime('%I:%M %p')}
        for m in msgs
    ])


# ---------------------------------------------------------
# ADMIN AUTH
# ---------------------------------------------------------
@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')

        admin = User.query.filter_by(email=email, role='admin').first()
        if admin and admin.check_password(password):
            login_user(admin)
            return redirect(url_for('admin_dashboard'))

        flash('Admin credentials ghalat hain.', 'error')
        return redirect(url_for('admin_login'))

    return render_template('admin/admin_login.html')


@app.route('/admin/logout')
@login_required
def admin_logout():
    logout_user()
    return redirect(url_for('admin_login'))


# ---------------------------------------------------------
# ADMIN DASHBOARD
# ---------------------------------------------------------
@app.route('/admin/dashboard')
@login_required
@admin_required
def admin_dashboard():
    total_students = User.query.filter_by(role='student').count()
    total_scholarships = Scholarship.query.count()
    unread_count = Message.query.filter_by(sender='user', is_read=False).count()
    return render_template(
        'admin/admin_dashboard.html',
        total_students=total_students,
        total_scholarships=total_scholarships,
        unread_count=unread_count,
    )


# ---------------------------------------------------------
# ADMIN: SCHOLARSHIPS CRUD
# ---------------------------------------------------------
@app.route('/admin/scholarships')
@login_required
@admin_required
def admin_scholarships():
    items = Scholarship.query.order_by(Scholarship.display_order).all()
    return render_template('admin/scholarships_manage.html', scholarships=items)


@app.route('/admin/scholarships/add', methods=['GET', 'POST'])
@login_required
@admin_required
def admin_scholarship_add():
    if request.method == 'POST':
        image_data = resize_and_encode_image(request.files.get('image'))
        item = Scholarship(
            title=request.form.get('title', '').strip(),
            country=request.form.get('country', '').strip(),
            description=request.form.get('description', ''),
            deadline=request.form.get('deadline', '').strip(),
            image_base64=image_data,
            size_option=request.form.get('size_option', 'medium'),
            display_order=int(request.form.get('display_order', 0) or 0),
            is_active=bool(request.form.get('is_active')),
        )
        db.session.add(item)
        db.session.commit()
        flash('Scholarship add ho gayi.', 'success')
        return redirect(url_for('admin_scholarships'))

    return render_template('admin/scholarship_form.html', scholarship=None)


@app.route('/admin/scholarships/<int:scholarship_id>/edit', methods=['GET', 'POST'])
@login_required
@admin_required
def admin_scholarship_edit(scholarship_id):
    item = Scholarship.query.get_or_404(scholarship_id)

    if request.method == 'POST':
        item.title = request.form.get('title', '').strip()
        item.country = request.form.get('country', '').strip()
        item.description = request.form.get('description', '')
        item.deadline = request.form.get('deadline', '').strip()
        item.size_option = request.form.get('size_option', 'medium')
        item.display_order = int(request.form.get('display_order', 0) or 0)
        item.is_active = bool(request.form.get('is_active'))

        new_image = resize_and_encode_image(request.files.get('image'))
        if new_image:
            item.image_base64 = new_image

        db.session.commit()
        flash('Scholarship update ho gayi.', 'success')
        return redirect(url_for('admin_scholarships'))

    return render_template('admin/scholarship_form.html', scholarship=item)


@app.route('/admin/scholarships/<int:scholarship_id>/delete', methods=['POST'])
@login_required
@admin_required
def admin_scholarship_delete(scholarship_id):
    item = Scholarship.query.get_or_404(scholarship_id)
    db.session.delete(item)
    db.session.commit()
    flash('Scholarship delete ho gayi.', 'success')
    return redirect(url_for('admin_scholarships'))


# ---------------------------------------------------------
# ADMIN: PAGE CONTENT EDITOR (WYSIWYG)
# ---------------------------------------------------------
EDITABLE_KEYS = [
    ('home_heading', 'Home Page Heading'),
    ('home_subtext', 'Home Page Subtext'),
    ('pricing_text', 'Pricing Page Content'),
    ('about_text', 'About Page Content'),
]


@app.route('/admin/content', methods=['GET', 'POST'])
@login_required
@admin_required
def admin_content():
    if request.method == 'POST':
        for key, _ in EDITABLE_KEYS:
            value = request.form.get(key, '')
            entry = PageContent.query.filter_by(key=key).first()
            if entry:
                entry.value = value
            else:
                entry = PageContent(key=key, value=value)
                db.session.add(entry)
        db.session.commit()
        flash('Content update ho gaya.', 'success')
        return redirect(url_for('admin_content'))

    current_values = {key: get_page_content(key, '') for key, _ in EDITABLE_KEYS}
    return render_template('admin/page_content_editor.html', keys=EDITABLE_KEYS, values=current_values)


# ---------------------------------------------------------
# ADMIN: CHAT INBOX (WhatsApp-style)
# ---------------------------------------------------------
@app.route('/admin/chat')
@login_required
@admin_required
def admin_chat_inbox():
    students = User.query.filter_by(role='student').all()
    inbox = []
    for s in students:
        last_msg = Message.query.filter_by(user_id=s.id).order_by(Message.timestamp.desc()).first()
        unread = Message.query.filter_by(user_id=s.id, sender='user', is_read=False).count()
        inbox.append({
            'user': s,
            'last_message': last_msg.text if last_msg else '',
            'last_time': last_msg.timestamp if last_msg else None,
            'unread': unread,
        })
    inbox.sort(key=lambda x: x['last_time'] or datetime.min, reverse=True)
    return render_template('admin/admin_chat_inbox.html', inbox=inbox)


@app.route('/admin/chat/<int:user_id>')
@login_required
@admin_required
def admin_chat_view(user_id):
    student = User.query.get_or_404(user_id)
    Message.query.filter_by(user_id=user_id, sender='user', is_read=False).update({'is_read': True})
    db.session.commit()
    return render_template('admin/admin_chat_conversation.html', student=student)


@app.route('/admin/chat/<int:user_id>/send', methods=['POST'])
@login_required
@admin_required
def admin_chat_send(user_id):
    text = request.form.get('text', '').strip()
    if text:
        msg = Message(user_id=user_id, sender='admin', text=text)
        db.session.add(msg)
        db.session.commit()
    return jsonify({'status': 'ok'})


@app.route('/admin/chat/<int:user_id>/messages')
@login_required
@admin_required
def admin_chat_messages(user_id):
    msgs = Message.query.filter_by(user_id=user_id).order_by(Message.timestamp).all()
    Message.query.filter_by(user_id=user_id, sender='user', is_read=False).update({'is_read': True})
    db.session.commit()
    return jsonify([
        {'sender': m.sender, 'text': m.text, 'timestamp': m.timestamp.strftime('%I:%M %p')}
        for m in msgs
    ])


# ---------------------------------------------------------
# DATABASE INIT (module level so it runs under waitress-serve on Render)
# ---------------------------------------------------------
def init_database():
    with app.app_context():
        db.create_all()
        # auto-create a default admin if none exists yet
        if not User.query.filter_by(role='admin').first():
            default_admin = User(name='Admin', email=DEFAULT_ADMIN_EMAIL, role='admin')
            default_admin.set_password(DEFAULT_ADMIN_PASSWORD)
            db.session.add(default_admin)
            db.session.commit()


init_database()


if __name__ == '__main__':
    app.run(debug=True)
