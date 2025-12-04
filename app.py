import os
import uuid
import io
from datetime import timedelta
from PIL import Image

from flask import Flask, request, jsonify, current_app, send_from_directory, Blueprint
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from flask_jwt_extended import JWTManager, create_access_token, jwt_required, get_jwt_identity
from werkzeug.utils import secure_filename
from sqlalchemy.orm import relationship

from flask_cors import CORS

# ---------- Configuration ----------
BASE_DIR = os.path.abspath(os.path.dirname(__file__))
UPLOAD_FOLDER = os.path.join(BASE_DIR, 'static', 'images')
if not os.path.exists(UPLOAD_FOLDER):
    os.makedirs(UPLOAD_FOLDER, exist_ok=True)

ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif'}
#ALLOWED_DOMAINS = ["@edu.tw", "@g.scvs.ntpc.edu.tw"]
MAX_IMAGES_PER_ITEM = 5

db = SQLAlchemy()
jwt = JWTManager()

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

# ---------- Models ----------
class User(db.Model):
    __tablename__ = 'user'
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password = db.Column(db.String(200), nullable=False)
    nickname = db.Column(db.String(50))
    contact = db.Column(db.String(100))
    grade = db.Column(db.String(20))
    department = db.Column(db.String(100))
    is_verified = db.Column(db.Boolean, default=True)
    is_admin = db.Column(db.Boolean, default=False)

    # relationships
    favorites = relationship("Favorite", backref="user", cascade="all, delete-orphan")

class Item(db.Model):
    __tablename__ = 'item'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    price = db.Column(db.Float, nullable=False)
    description = db.Column(db.String(255))
    category = db.Column(db.String(50), nullable=False)
    condition = db.Column(db.String(50), default="良好")
    status = db.Column(db.String(20), default="上架中")
    seller_email = db.Column(db.String(120), db.ForeignKey('user.email'), nullable=False)
    created_at = db.Column(db.DateTime, server_default=db.func.now())

    images = relationship('ItemImage', backref='item', cascade='all, delete-orphan')

class ItemImage(db.Model):
    __tablename__ = 'item_image'
    id = db.Column(db.Integer, primary_key=True)
    item_id = db.Column(db.Integer, db.ForeignKey('item.id', ondelete="CASCADE"), nullable=False)
    image_url = db.Column(db.String(255), nullable=False)
    order = db.Column(db.Integer, default=0)

class Favorite(db.Model):
    __tablename__ = 'favorite'
    id = db.Column(db.Integer, primary_key=True)
    user_email = db.Column(db.String(120), db.ForeignKey('user.email'), nullable=False)
    item_id = db.Column(db.Integer, db.ForeignKey('item.id'), nullable=False)

class Review(db.Model):
    __tablename__ = 'review'
    id = db.Column(db.Integer, primary_key=True)
    reviewer_email = db.Column(db.String(120), nullable=False)
    target_email = db.Column(db.String(120), nullable=False)
    rating = db.Column(db.Integer, nullable=False)
    comment = db.Column(db.String(255))
    created_at = db.Column(db.DateTime, server_default=db.func.now())

class Transaction(db.Model):
    __tablename__ = 'transaction'
    id = db.Column(db.Integer, primary_key=True)
    item_id = db.Column(db.Integer, db.ForeignKey('item.id'))
    buyer_email = db.Column(db.String(120), db.ForeignKey('user.email'))
    seller_email = db.Column(db.String(120), db.ForeignKey('user.email'))
    status = db.Column(db.String(20), default='洽談中')
    created_at = db.Column(db.DateTime, server_default=db.func.now())

class Report(db.Model):
    __tablename__ = 'report'
    id = db.Column(db.Integer, primary_key=True)
    reporter_email = db.Column(db.String(120), nullable=False)
    target_type = db.Column(db.String(20))  # 'item' | 'user'
    target_id = db.Column(db.Integer)
    reason = db.Column(db.String(255))
    status = db.Column(db.String(20), default='待處理')
    created_at = db.Column(db.DateTime, server_default=db.func.now())

# ---------- Helpers ----------
def get_current_user_obj():
    email = get_jwt_identity()
    if not email:
        return None
    return User.query.filter_by(email=email.lower()).first()

def require_admin(user):
    return user and user.is_admin

def save_uploaded_file(file_storage):
    """
    Save uploaded file after verifying it's a valid image.
    Returns the public URL path (e.g. /static/images/xxx) on success, or None on failure.
    """
    filename = getattr(file_storage, 'filename', '')
    if not filename or not allowed_file(filename):
        return None
    filename = secure_filename(filename)
    new_filename = f"{uuid.uuid4().hex}_{filename}"
    dest_path = os.path.join(current_app.config['UPLOAD_FOLDER'], new_filename)

    try:
        # Read bytes
        file_bytes = file_storage.read()
        # Verify image using PIL
        img = Image.open(io.BytesIO(file_bytes))
        img.verify()  # will raise if not a valid image

        # Write raw bytes to disk
        with open(dest_path, 'wb') as f:
            f.write(file_bytes)

        # Optionally you could re-open and re-save to normalize format/strip metadata:
        # try:
        #     img2 = Image.open(dest_path).convert("RGB")
        #     img2.save(dest_path, format='JPEG', quality=85)
        # except Exception:
        #     pass

        # Reset stream if downstream code expects it (best-effort)
        try:
            file_storage.stream = io.BytesIO(file_bytes)
        except Exception:
            pass

    except Exception as e:
        current_app.logger.warning(f"上傳檔案驗證失敗: {e}")
        return None

    return f"/static/images/{new_filename}"

def delete_local_file_if_exists(image_url):
    if not image_url:
        return
    prefix = "/static/images/"
    if image_url.startswith(prefix):
        filename = image_url[len(prefix):]
        path = os.path.join(current_app.config['UPLOAD_FOLDER'], filename)
        try:
            if os.path.exists(path):
                os.remove(path)
        except Exception:
            current_app.logger.exception("刪除本地檔案失敗")

# ---------- Blueprints ----------
auth_bp = Blueprint('auth', __name__, url_prefix='/auth')
user_bp = Blueprint('user', __name__, url_prefix='/user')
items_bp = Blueprint('items', __name__, url_prefix='/items')
fav_bp = Blueprint('favorite', __name__, url_prefix='/favorite')
review_bp = Blueprint('review', __name__, url_prefix='/review')
tx_bp = Blueprint('transaction', __name__, url_prefix='/transaction')
admin_bp = Blueprint('admin', __name__, url_prefix='/admin')

# ---- Auth routes ----
@auth_bp.route('/test_db')
def test_db():
    try:
        user_count = User.query.count()
        return jsonify({"message": f"資料庫連線成功，目前 User 表有 {user_count} 筆資料"}), 200
    except Exception as e:
        return jsonify({"message": f"資料庫連線失敗，錯誤：{str(e)}"}), 500

@auth_bp.route('/register', methods=['POST'])
def register():
    print('api 收到 /auth/register')
    try:
        data = request.get_json() or {}
        email = (data.get("email") or "").strip().lower()
        password = data.get("password")

        if not email or not password:
            return jsonify({"message": "請提供 email 與密碼"}), 400

        is_school_email = ("@" in email) and email.endswith(".edu.tw")

        if not is_school_email:
            return jsonify({"message": f"請使用學校信箱"}), 400

        if User.query.filter_by(email=email).first():
            return jsonify({"message": "此 email 已被註冊"}), 400

        hashed = generate_password_hash(password)
        is_admin = (User.query.count() == 0)
        user = User(email=email, password=hashed, is_admin=is_admin, is_verified=True)
        db.session.add(user)
        db.session.commit()

        return jsonify({"message": "註冊成功，已為已驗證，可直接登入"}), 201
    except Exception as e:
        current_app.logger.exception("register handler error")
        return jsonify({"message": "伺服器內部錯誤"}), 500


@auth_bp.route('/login', methods=['POST'])
def login():
    print('api 收到 /login')
    data = request.get_json() or {}
    email = (data.get("email") or "").strip().lower()
    password = data.get("password")
    if not email or not password:
        return jsonify({"message": "請提供 email 與密碼"}), 400
    user = User.query.filter_by(email=email).first()
    if not user or not check_password_hash(user.password, password):
        return jsonify({"message": "帳號或密碼錯誤"}), 401
    if not user.is_verified:
        return jsonify({"message": "請先驗證 email"}), 403
    access_token = create_access_token(identity=user.email, expires_delta=timedelta(days=7))
    return jsonify({"access_token": access_token, "is_admin": user.is_admin}), 200

@auth_bp.route('/logout', methods=['POST'])
@jwt_required()
def logout():
    return jsonify({"message": "已登出，前端請刪除或清空 token"}), 200

@auth_bp.route('/change_password', methods=['POST'])
@jwt_required()
def change_password():
    current_user = get_current_user_obj()
    if not current_user:
        return jsonify({"message": "找不到使用者"}), 404
    data = request.get_json() or {}
    old_password = data.get("old_password")
    new_password = data.get("new_password")
    if not old_password or not new_password:
        return jsonify({"message": "請提供舊密碼與新密碼"}), 400
    if not check_password_hash(current_user.password, old_password):
        return jsonify({"message": "舊密碼錯誤"}), 403
    current_user.password = generate_password_hash(new_password)
    db.session.commit()
    return jsonify({"message": "密碼已更新"}), 200

@auth_bp.route('/reset_password_direct', methods=['POST'])
def reset_password_direct():
    print('api 收到 /reset_password_direct')
    data = request.get_json() or {}
    email = (data.get("email") or "").strip().lower()
    new_password = data.get("new_password")
    if not email or not new_password:
        return jsonify({"message": "請提供 email 與 new_password"}), 400
    user = User.query.filter_by(email=email).first()
    if not user:
        return jsonify({"message": "找不到使用者"}), 404
    user.password = generate_password_hash(new_password)
    db.session.commit()
    return jsonify({"message": "密碼已重設成功"}), 200

# ---- User profile routes ----
@user_bp.route('/profile', methods=['GET'])
@jwt_required()
def get_profile(): 
    print('api 收到 /profile GET')    
    user = get_current_user_obj()
    if not user:
        return jsonify({"message": "找不到使用者"}), 404
    return jsonify({
        "email": user.email,
        "nickname": user.nickname,
        "contact": user.contact,
        "grade": user.grade,
        "department": user.department,
        "is_verified": user.is_verified,
        "is_admin": user.is_admin
    }), 200

@user_bp.route('/profile', methods=['PUT'])
@jwt_required()
def update_profile():
    user = get_current_user_obj()
    if not user:
        return jsonify({"message": "找不到使用者"}), 404
    data = request.get_json() or {}
    for fld in ("nickname", "contact", "grade", "department"):
        if fld in data:
            setattr(user, fld, data[fld])
    db.session.commit()
    return jsonify({"message": "個人資料已更新"}), 200

# ---- Items routes ----
@items_bp.route('/upload', methods=['POST'])
@jwt_required()
def upload_item():
    print('api 收到 /upload')    
    current_user = get_current_user_obj()
    if not current_user:
        return jsonify({"message": "找不到使用者"}), 404

    if request.mimetype and request.mimetype.startswith('multipart/form-data'):
        name = request.form.get("name")
        price = request.form.get("price")
        category = request.form.get("category")
        description = request.form.get("description")
        condition = request.form.get("condition")
        files = request.files.getlist("images") or []

        if not name or price is None or not category:
            return jsonify({"message": "請提供必要欄位"}), 400
        try:
            price_val = float(price)
        except (TypeError, ValueError):
            return jsonify({"message": "價格格式錯誤"}), 400

        if len(files) > MAX_IMAGES_PER_ITEM:
            return jsonify({"message": f"最多只能上傳 {MAX_IMAGES_PER_ITEM} 張圖片"}), 400

        item = Item(
            name=name,
            price=price_val,
            category=category,
            description=description,
            condition=condition or "良好",
            seller_email=current_user.email
        )
        db.session.add(item)
        db.session.flush()

        order_idx = 0
        for f in files:
            if f and allowed_file(f.filename):
                url = save_uploaded_file(f)
                if url:
                    img = ItemImage(item_id=item.id, image_url=url, order=order_idx)
                    db.session.add(img)
                    order_idx += 1

        db.session.commit()
        return jsonify({"message": "商品上架成功", "item_id": item.id}), 201

    else:
        data = request.get_json()
        if not data:
            return jsonify({"message": "請提供完整商品資訊"}), 400
        required = ["name", "price", "category"]
        if any(data.get(f) is None for f in required):
            return jsonify({"message": "請提供完整商品資訊"}), 400
        try:
            price_val = float(data["price"])
        except (TypeError, ValueError):
            return jsonify({"message": "價格格式錯誤"}), 400

        image_urls = data.get("image_urls", []) or []
        if len(image_urls) > MAX_IMAGES_PER_ITEM:
            return jsonify({"message": f"最多只能提供 {MAX_IMAGES_PER_ITEM} 張 image_urls"}), 400

        item = Item(
            name=data["name"],
            price=price_val,
            description=data.get("description"),
            category=data["category"],
            condition=data.get("condition", "良好"),
            seller_email=current_user.email
        )
        db.session.add(item)
        db.session.flush()

        order_idx = 0
        for url in image_urls:
            img = ItemImage(item_id=item.id, image_url=url, order=order_idx)
            db.session.add(img)
            order_idx += 1

        db.session.commit()
        return jsonify({"message": "商品上架成功（JSON）", "item_id": item.id}), 201

@items_bp.route('/', methods=['GET'])
def list_items():
    print('收到 /items')  
    page = request.args.get("page", default=1, type=int)
    per_page = request.args.get("per_page", default=20, type=int)
    #print(f'page={page}')
    #print(f'per_page={per_page}')
    if per_page > 100:
        per_page = 100

    category = request.args.get("category")
    name = request.args.get("name")
    status = request.args.get("status")
    condition = request.args.get("condition")
    min_price = request.args.get("min_price", type=float)
    max_price = request.args.get("max_price", type=float)
    seller_email = request.args.get("seller_email")
    if category == "全部商品" : category = "" 

    query = Item.query
    if category: query = query.filter(Item.category.like(f"%{category}%"))
    if name: query = query.filter(Item.name.like(f"%{name}%"))
    if status: query = query.filter(Item.status == status)
    if condition: query = query.filter(Item.condition == condition)
    if min_price is not None: query = query.filter(Item.price >= min_price)
    if max_price is not None: query = query.filter(Item.price <= max_price)
    if seller_email: query = query.filter(Item.seller_email == seller_email)
   
    pagination = query.order_by(Item.created_at.desc()).paginate(page=page, per_page=per_page, error_out=False)
    items = pagination.items

    results = []
    for i in items:
        images = ItemImage.query.filter_by(item_id=i.id).order_by(ItemImage.order.asc()).all()
        image_urls = [img.image_url for img in images]
        results.append({
            "id": i.id,
            "name": i.name,
            "price": i.price,
            "description": i.description,
            "category": i.category,
            "condition": i.condition,
            "images": image_urls,
            "status": i.status,
            "seller_email": i.seller_email,
            # 使用 ISO 格式，讓前端以 new Date(...) 解析更一致
            "created_at": i.created_at.isoformat() if i.created_at else None
        })
    return jsonify({
        "items": results,
        "page": page,
        "per_page": per_page,
        "total": pagination.total
    }), 200

@items_bp.route('/<int:item_id>', methods=['GET'])
def get_item(item_id):
    i = Item.query.get(item_id)
    print(f'api 收到[GET] /item_id={item_id}')  
    if not i:
        return jsonify({"message": "找不到商品"}), 404
    images = ItemImage.query.filter_by(item_id=i.id).order_by(ItemImage.order.asc()).all()
    image_urls = [img.image_url for img in images]
    return jsonify({
        "id": i.id, "name": i.name, "price": i.price, "description": i.description,
        "category": i.category, "condition": i.condition, "images": image_urls, "status": i.status,
        "seller_email": i.seller_email, "created_at": i.created_at.isoformat() if i.created_at else None
    }), 200



@items_bp.route('/<int:item_id>', methods=['PUT'])
@jwt_required()
def update_item(item_id):
    print('api 收到[PUT] /item_id')  
    current_user = get_current_user_obj()
    if not current_user:
        return jsonify({"message": "找不到使用者"}), 404
    item = Item.query.get(item_id)
    if not item:
        return jsonify({"message": "找不到商品"}), 404
    if current_user.email != item.seller_email and not current_user.is_admin:
        return jsonify({"message": "沒有權限修改"}), 403

    if request.mimetype and request.mimetype.startswith('multipart/form-data'):
        data = request.form or {}
        files = request.files.getlist("images") or []
        for k in ["name", "description", "category", "status", "condition"]:
            if k in data:
                setattr(item, k, data[k])
        if "price" in data:
            try:
                item.price = float(data["price"])
            except (TypeError, ValueError):
                return jsonify({"message": "價格格式錯誤"}), 400

        existing_count = ItemImage.query.filter_by(item_id=item.id).count()
        if existing_count + len(files) > MAX_IMAGES_PER_ITEM:
            return jsonify({"message": f"總圖片數不可超過 {MAX_IMAGES_PER_ITEM} 張"}), 400

        order_idx = existing_count
        for f in files:
            if f and allowed_file(f.filename):
                url = save_uploaded_file(f)
                if url:
                    img = ItemImage(item_id=item.id, image_url=url, order=order_idx)
                    db.session.add(img)
                    order_idx += 1

        db.session.commit()
        return jsonify({"message": "商品更新成功"}), 200

    else:
        data = request.get_json() or {}
        for k in ["name", "description", "category", "status", "condition"]:
            if k in data:
                setattr(item, k, data[k])
        if "price" in data:
            try:
                item.price = float(data["price"])
            except (TypeError, ValueError):
                return jsonify({"message": "價格格式錯誤"}), 400

        if "image_urls" in data:
            image_urls = data.get("image_urls") or []
            if len(image_urls) > MAX_IMAGES_PER_ITEM:
                return jsonify({"message": f"最多只能提供 {MAX_IMAGES_PER_ITEM} 張圖片"}), 400
            images = ItemImage.query.filter_by(item_id=item.id).all()
            for img in images:
                delete_local_file_if_exists(img.image_url)
            ItemImage.query.filter_by(item_id=item.id).delete()
            order_idx = 0
            for url in image_urls:
                img = ItemImage(item_id=item.id, image_url=url, order=order_idx)
                db.session.add(img)
                order_idx += 1

        db.session.commit()
        return jsonify({"message": "商品更新成功"}), 200

@items_bp.route('/<int:item_id>', methods=['DELETE'])
@jwt_required()
def delete_item(item_id):
    print('api 收到[DELETE] /item_id')  
    current_user = get_current_user_obj()
    if not current_user:
        return jsonify({"message": "找不到使用者"}), 404
    item = Item.query.get(item_id)
    if not item:
        return jsonify({"message": "找不到商品"}), 404
    if current_user.email != item.seller_email and not current_user.is_admin:
        return jsonify({"message": "沒有權限刪除"}), 403

    images = ItemImage.query.filter_by(item_id=item.id).all()
    for img in images:
        delete_local_file_if_exists(img.image_url)
    ItemImage.query.filter_by(item_id=item.id).delete()

    db.session.delete(item)
    db.session.commit()
    return jsonify({"message": "商品已刪除"}), 200

# ----- 540 用seller_email找尋刊登商品 ---
'''
@items_bp.route('/<seller_email>', methods=['GET'])
def get_mysells(user_email):
    print('api def get_mysells')  
    sellers = Item.query.filter_by(seller_email=user_email).all()
    result = []
    for f in sellers:
            trans =  Transaction.query.filter_by(item_id=f.item_id).all()
            buyer_emails =[s.buyer_email for s in trans]

            images = ItemImage.query.filter_by(item_id=f.item_id).order_by(ItemImage.order.asc()).all()
            image_urls = [img.image_url for img in images]
            result.append({
                "id": f.item_id,
                "name": f.name,
                "price": f.price,
                "description": f.description,
                "condition": f.condition,
                "images": image_urls,
                "buyer_email": buyer_emails
            })
    return jsonify(result), 200
'''

# ---- Favorites ----
@fav_bp.route('/', methods=['POST'])
@jwt_required()
def add_favorite():
    print('api def add_favorite')  
    current_user = get_current_user_obj()
    if not current_user:
        return jsonify({"message": "找不到使用者"}), 404
    data = request.get_json() or {}
    item_id = data.get("item_id")
    if not item_id:
        return jsonify({"message": "請提供 item_id"}), 400
    if Favorite.query.filter_by(user_email=current_user.email, item_id=item_id).first():
        return jsonify({"message": "已收藏"}), 400
    fav = Favorite(user_email=current_user.email, item_id=item_id)
    db.session.add(fav)
    db.session.commit()
    return jsonify({"message": "收藏成功"}), 201

@fav_bp.route('/', methods=['DELETE'])
@jwt_required()
def remove_favorite():
    print('api def remove_favorite')  
    current_user = get_current_user_obj()
    if not current_user:
        return jsonify({"message": "找不到使用者"}), 404
    if request.is_json:
        data = request.get_json() or {}
        item_id = data.get("item_id")
    else:
        item_id = request.args.get("item_id", type=int)
    if not item_id:
        return jsonify({"message": "請提供 item_id"}), 400
    fav = Favorite.query.filter_by(user_email=current_user.email, item_id=item_id).first()
    if not fav:
        return jsonify({"message": "找不到收藏"}), 404
    db.session.delete(fav)
    db.session.commit()
    return jsonify({"message": "已取消收藏"}), 200

@fav_bp.route('/list/<user_email>', methods=['GET'])
def get_favorites(user_email):
    print('api def get_favorites')  
    favs = Favorite.query.filter_by(user_email=user_email).all()
    result = []
    for f in favs:
        item = Item.query.get(f.item_id)
        if item:
            images = ItemImage.query.filter_by(item_id=item.id).order_by(ItemImage.order.asc()).all()
            image_urls = [img.image_url for img in images]
            result.append({
                "id": item.id,
                "name": item.name,
                "price": item.price,
                "description": item.description,
                "condition": item.condition,
                "images": image_urls,
                "seller_email": item.seller_email
            })
    return jsonify(result), 200

# ---- Review ----
@review_bp.route('/', methods=['POST'])
@jwt_required()
def create_review():
    print('api create_review')  
    current_user = get_current_user_obj()
    if not current_user:
        return jsonify({"message": "找不到使用者"}), 404
    data = request.get_json() or {}
    if not data.get("target_email") or data.get("rating") is None:
        return jsonify({"message": "請提供被評價者與分數"}), 400
    review = Review(
        reviewer_email=current_user.email,
        target_email=data["target_email"],
        rating=int(data["rating"]),
        comment=data.get("comment")
    )
    db.session.add(review)
    db.session.commit()
    return jsonify({"message": "評價已送出"}), 201

@review_bp.route('/<target_email>', methods=['GET'])
def get_reviews(target_email):
    reviews = Review.query.filter_by(target_email=target_email).order_by(Review.created_at.desc()).all()
    avg = db.session.query(db.func.avg(Review.rating)).filter_by(target_email=target_email).scalar() or 0
    return jsonify({
        "average_rating": round(avg, 2),
        "reviews": [{
            "reviewer_email": r.reviewer_email,
            "rating": r.rating,
            "comment": r.comment,
            "created_at": r.created_at.isoformat() if r.created_at else None
        } for r in reviews]
    }), 200

# ---- Transaction ----
@tx_bp.route('/', methods=['POST'])
@jwt_required()
def create_transaction():
    print('api create_transaction')
    current_user = get_current_user_obj()
    if not current_user:
        return jsonify({"message": "找不到使用者"}), 404
    data = request.get_json() or {}
    item_id = data.get("item_id")
    if not item_id:
        return jsonify({"message": "請提供 item_id"}), 400
    buyer_email = current_user.email
    item = Item.query.get(item_id)
    if not item:
        return jsonify({"message": "找不到商品"}), 404
    if item.seller_email == buyer_email:
        return jsonify({"message": "不能購買自己的商品"}), 400
    if item.status != "上架中":
        return jsonify({"message": f"此商品目前無法購買（狀態：{item.status}）"}), 400

    tx = Transaction(item_id=item_id, buyer_email=buyer_email, seller_email=item.seller_email, status="洽談中")
    item.status = "洽談中"
    db.session.add(tx)
    db.session.commit()
    return jsonify({"message": "交易已建立", "transaction_id": tx.id}), 201

@tx_bp.route('/<int:tx_id>/status', methods=['PATCH'])
@jwt_required()
def update_transaction_status(tx_id):
    print('api update_transaction_status')
    current_user = get_current_user_obj()
    if not current_user:
        return jsonify({"message": "找不到使用者"}), 404
    tx = Transaction.query.get(tx_id)
    if not tx:
        return jsonify({"message": "找不到交易"}), 404
    if current_user.email not in [tx.buyer_email, tx.seller_email] and not current_user.is_admin:
        return jsonify({"message": "沒有權限修改交易狀態"}), 403
    new_status = (request.get_json() or {}).get("status")
    if not new_status:
        return jsonify({"message": "請提供新的狀態"}), 400
    tx.status = new_status
    if new_status == "已成交":
        item = Item.query.get(tx.item_id)
        if item:
            item.status = "已成交"
    db.session.commit()
    return jsonify({"message": "交易狀態已更新"}), 200

@tx_bp.route('/list/<email>', methods=['GET'])
@jwt_required()
def get_transactions_for(email):
    print('api get_transactions_for')
    current_user = get_current_user_obj()
    if not current_user:
        return jsonify({"message": "找不到使用者"}), 404
    if current_user.email != email and not current_user.is_admin:
        return jsonify({"message": "沒有權限查詢"}), 403
    txs = Transaction.query.filter((Transaction.buyer_email == email) | (Transaction.seller_email == email)).order_by(Transaction.created_at.desc()).all()
    results = []
    for t in txs:
        item = Item.query.get(t.item_id)  # ← 取得 item 資料
        item_name = item.name if item else None

        results.append({
            "id": t.id,
            "item_id": t.item_id,
            "item_name": item_name,   # ← 新增
            "buyer_email": t.buyer_email,
            "seller_email": t.seller_email,
            "status": t.status,
            "created_at": t.created_at.isoformat() if t.created_at else None
        })

    return jsonify(results), 200
    '''return jsonify([{
        "id": t.id,
        "item_id": t.item_id,
        "buyer_email": t.buyer_email,
        "seller_email": t.seller_email,
        "status": t.status,
        "created_at": t.created_at.isoformat() if t.created_at else None
    } for t in txs]), 200'''


# ---- Reports & Admin ----
@admin_bp.route('/report', methods=['POST'])
@jwt_required()
def create_report():
    current_user = get_current_user_obj()
    if not current_user:
        return jsonify({"message": "找不到使用者"}), 404
    data = request.get_json() or {}
    if not all([data.get("target_type"), data.get("target_id"), data.get("reason")]):
        return jsonify({"message": "請提供被檢舉目標與理由"}), 400
    rpt = Report(reporter_email=current_user.email,
                 target_type=data["target_type"],
                 target_id=data["target_id"],
                 reason=data["reason"])
    db.session.add(rpt)
    db.session.commit()
    return jsonify({"message": "檢舉已送出"}), 201

@admin_bp.route('/reports', methods=['GET'])
@jwt_required()
def admin_get_reports():
    current_user = get_current_user_obj()
    if not current_user:
        return jsonify({"message": "找不到使用者"}), 404
    if not require_admin(current_user):
        return jsonify({"message": "需管理員權限"}), 403
    reports = Report.query.order_by(Report.created_at.desc()).all()
    return jsonify([{
        "id": r.id,
        "reporter_email": r.reporter_email,
        "target_type": r.target_type,
        "target_id": r.target_id,
        "reason": r.reason,
        "status": r.status,
        "created_at": r.created_at.isoformat() if r.created_at else None
    } for r in reports]), 200

@admin_bp.route('/report/<int:report_id>', methods=['PATCH'])
@jwt_required()
def admin_update_report(report_id):
    current_user = get_current_user_obj()
    if not current_user:
        return jsonify({"message": "找不到使用者"}), 404
    if not require_admin(current_user):
        return jsonify({"message": "需管理員權限"}), 403
    rpt = Report.query.get(report_id)
    if not rpt:
        return jsonify({"message": "找不到檢舉"}), 404
    new_status = (request.get_json() or {}).get("status")
    if new_status:
        rpt.status = new_status
    db.session.commit()
    return jsonify({"message": "檢舉狀態已更新"}), 200

@admin_bp.route('/item/<int:item_id>', methods=['DELETE'])
@jwt_required()
def admin_delete_item(item_id):
    current_user = get_current_user_obj()
    if not current_user:
        return jsonify({"message": "找不到使用者"}), 404
    if not require_admin(current_user):
        return jsonify({"message": "需管理員權限"}), 403
    item = Item.query.get(item_id)
    if not item:
        return jsonify({"message": "找不到商品"}), 404

    Favorite.query.filter_by(item_id=item_id).delete()
    Transaction.query.filter_by(item_id=item_id).delete()

    images = ItemImage.query.filter_by(item_id=item_id).all()
    for img in images:
        delete_local_file_if_exists(img.image_url)
    ItemImage.query.filter_by(item_id=item_id).delete()

    db.session.delete(item)
    db.session.commit()
    return jsonify({"message": "已刪除商品"}), 200

# ---------- Static file route (uploads) ----------
def uploads_route(app):
    print('api uploads_route')
    @app.route('/static/images/<filename>')
    def uploaded_file(filename):
        return send_from_directory(app.config['UPLOAD_FOLDER'], filename)

# ---------- Application Factory ----------
def create_app(test_config=None):
    app = Flask(__name__)
    app.config['JSON_AS_ASCII'] = False

    app.config["SQLALCHEMY_DATABASE_URI"] = os.getenv("DATABASE_URL", "mysql+pymysql://root:r0gerd0rac0ni@localhost/campus_market")
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

    app.config["JWT_SECRET_KEY"] = os.getenv("JWT_SECRET_KEY", "dev_secret")
    app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

    app.config['MAX_CONTENT_LENGTH'] = 8 * 1024 * 1024

    # JWT 設定（可用環境變數覆蓋）
    app.config['JWT_ACCESS_TOKEN_EXPIRES'] = timedelta(days=7)

    db.init_app(app)
    jwt.init_app(app)

    # 統一 CORS 設定：允許前端傳 Authorization header 與多種 HTTP method
    CORS(app, resources={r"/*": {"origins": "*"}},
         supports_credentials=True,
         allow_headers=["Content-Type", "Authorization", "Access-Control-Allow-Credentials"],
         methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])

    # register blueprints
    app.register_blueprint(auth_bp)
    app.register_blueprint(user_bp)
    app.register_blueprint(items_bp)
    app.register_blueprint(fav_bp)
    app.register_blueprint(review_bp)
    app.register_blueprint(tx_bp)
    app.register_blueprint(admin_bp)

    uploads_route(app)

    @app.errorhandler(Exception)
    def handle_error(e):
        if app.config.get("DEBUG"):
            return jsonify({"error": str(e)}), 500
        else:
            current_app.logger.exception(e)
            return jsonify({"error": "伺服器發生錯誤"}), 500

    @app.route('/', defaults={'path': ''})
    @app.route('/<path:path>')
    def serve_frontend(path):
        if path.startswith("static/") or path.startswith("api/") or path.startswith("auth/"):
            return jsonify({"message": "Invalid path"}), 404
        return jsonify({"message": "後端運作正常，請從前端啟動 React App"}), 200

    with app.app_context():
        db.create_all()

    return app

# ---------- Run ----------
if __name__ == '__main__':
    app = create_app()
    app.run(host='0.0.0.0', port=int(os.getenv("PORT", 5000)), debug=True)