from flask import Flask, render_template, request, redirect, url_for, session, flash, send_from_directory, jsonify, Response
from flask_pymongo import PyMongo
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
import re
from dotenv import load_dotenv
from pymongo import ASCENDING
import pymongo
from urllib.parse import urlparse
import razorpay
from bson.objectid import ObjectId
import os
from datetime import datetime
import math
import json
import random
import traceback

# Load env variables
load_dotenv()

# Get the absolute path for templates
template_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), 'templets'))
app = Flask(__name__, template_folder=template_dir)
app.secret_key = os.getenv("SECRET_KEY", "dev-secret-key-change-in-production")

# Razorpay Setup - Loading from .env with priority
RAZORPAY_KEY_ID = os.getenv("RAZORPAY_KEY_ID")
RAZORPAY_KEY_SECRET = os.getenv("RAZORPAY_KEY_SECRET")

# ROBUST FALLBACK FOR DEPLOYMENT: Enforce specific keys if env vars are missing or empty
if not RAZORPAY_KEY_ID or not RAZORPAY_KEY_SECRET:
    print("⚠️ WARNING: Razorpay Env Vars missing! Please set RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET in your environment variables.")
    # We do NOT fallback to hardcoded keys as per request to keep secrets in .env only
    pass

if RAZORPAY_KEY_ID:
    print(f"DEBUG: Using Razorpay Key: {RAZORPAY_KEY_ID[:10]}...")

try:
    razorpay_client = razorpay.Client(auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET))
    # Test keys immediately
    razorpay_client.order.create({"amount": 100, "currency": "INR"})
    print(f"✅ Razorpay Authenticated Successfully (ID: {RAZORPAY_KEY_ID[:12]}...)")
except razorpay.errors.BadRequestError:
    print(f"❌ Razorpay AUTHENTICATION FAILED: Check your key_id and key_secret in .env")
    razorpay_client = None
except Exception as e:
    print(f"⚠️ Razorpay Setup Warning: {e}")
    # We still keep the client if it's just a network issue
    if 'razorpay_client' not in locals(): razorpay_client = None

import requests

# Shiprocket Setup
SHIPROCKET_EMAIL = os.getenv("SHIPROCKET_EMAIL")
SHIPROCKET_PASSWORD = os.getenv("SHIPROCKET_PASSWORD")
shiprocket_token = None

def get_shiprocket_token():
    global shiprocket_token
    try:
        url = "https://apiv2.shiprocket.in/v1/external/auth/login"
        payload = {
            "email": SHIPROCKET_EMAIL,
            "password": SHIPROCKET_PASSWORD
        }
        response = requests.post(url, json=payload)
        if response.status_code == 200:
            shiprocket_token = response.json().get('token')
            print("✅ Shiprocket Authenticated Successfully")
            return shiprocket_token
        else:
            print(f"❌ Shiprocket Auth Failed: {response.text}")
            return None
    except Exception as e:
        print(f"⚠️ Shiprocket Setup Error: {e}")
        return None

# Attempt initial login
if SHIPROCKET_EMAIL and SHIPROCKET_PASSWORD:
    get_shiprocket_token()

# MongoDB connection
mongo_uri = os.getenv("MONGO_URI")
db = None

def ensure_db_connection():
    global db
    if db is not None:
        return db
    
    uri = os.getenv("MONGO_URI")
    if not uri:
        print("CRITICAL ERROR: MONGO_URI not found in environment variables!")
        return None
        
    try:
        client = pymongo.MongoClient(uri, serverSelectionTimeoutMS=5000)
        try:
            db = client.get_default_database()
        except Exception:
            parsed = urlparse(uri)
            dbname = parsed.path.lstrip('/') or 'KropKart'
            db = client[dbname]
        
        # Verify connection
        client.admin.command('ping')
        print(f"Connected to database: {db.name}")
        return db
    except Exception as e:
        print(f"Database Connection Error: {e}")
        traceback.print_exc()
        return None

def init_db():
    db_local = ensure_db_connection()
    if db_local is not None:
        collections = ["users", "products", "orders", "categories", "shipments", "admin"]
        for collection in collections:
            if collection not in db_local.list_collection_names():
                db_local.create_collection(collection)
        db_local.users.create_index([("email", ASCENDING)], unique=True)
        return True
    return False

# AI Analysis Logic
def analyze_quality(name, description, category, price):
    score = 0.5
    text = f"{name} {description} {category}".lower()
    if 'organic' in text: score += 0.2
    if 'premium' in text or 'pure' in text: score += 0.15
    if 'grade a' in text: score += 0.1
    return min(1.0, score)

def get_quality_label(score):
    if score >= 0.9: return "Premium Grade"
    if score >= 0.8: return "High Quality"
    if score >= 0.6: return "Standard Grade"
    return "Fair Quality"

def compute_adjusted_price(base_price, quality_score):
    gov_rate = 0.05
    bonus = 0.05 if quality_score > 0.8 else 0.02 if quality_score > 0.5 else 0
    return round(float(base_price) * (1 + gov_rate + bonus), 2)

@app.route('/statics/<path:filename>')
def serve_statics(filename):
    return send_from_directory(os.path.join(os.path.dirname(__file__), 'statics'), filename)

@app.route("/refund-policy")
def refund_policy():
    return render_template("refund_policy.html")

@app.route("/")
def index():
    db_local = ensure_db_connection()
    products = list(db_local.products.find().sort("created_at", -1))
    for p in products:
        p['_id'] = str(p['_id'])
        q = p.get('quality_score', 0.8)
        p['quality_label'] = p.get('user_quality') or get_quality_label(q)
        # Map 0.5-1.0 to 3.5-5.0 + 10% random boost
        base_rating = 3.5 + (max(0, q-0.5) / 0.5) * 1.5
        p['rating'] = round(min(5.0, base_rating * (1 + random.uniform(0.05, 0.10))), 1)
    return render_template("index.html", products=products)

@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        name = request.form["name"]
        email = request.form["email"].strip().lower()
        password = request.form["password"]
        user_type = request.form.get("user_type", "citizen")
        
        db_local = ensure_db_connection()
        if db_local.users.find_one({"email": email}):
            flash("User already exists!", "error")
            return redirect("/register")
            
        # Generate specialized IDs for Farmer and Business
        user_id = None
        if user_type == "farmer":
            user_id = f"FRM-{random.randint(100000, 999999)}"
        elif user_type == "business":
            user_id = f"BUS-{random.randint(100000, 999999)}"
            
        db_local.users.insert_one({
            "name": name, 
            "email": email, 
            "password": generate_password_hash(password),
            "user_type": user_type, 
            "user_id": user_id, # Store the generated ID
            "created_at": datetime.now(), 
            "wallet": 0
        })
        
        flash_msg = "Registration successful!"
        if user_id:
            flash_msg += f" Your {user_type.capitalize()} ID is {user_id}"
        
        flash(flash_msg, "success")
        return redirect("/login")
    return render_template("register.html")

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form["email"].strip().lower()
        password = request.form["password"]
        db_local = ensure_db_connection()
        user = db_local.users.find_one({"email": email})
        if user and check_password_hash(user["password"], password):
            session.update({
                "user": user["email"], 
                "name": user["name"], 
                "user_type": user["user_type"],
                "user_id": user.get("user_id") # Store specialized ID in session
            })
            flash(f"Welcome back, {user['name']}!", "success")
            return redirect("/")  # Redirect to home page
        flash("Invalid credentials", "error")
    return render_template("login.html")

@app.route("/dashboard")
def dashboard():
    if "user" not in session: return redirect("/login")
    utype = session.get("user_type")
    if utype == "admin": return redirect("/admin")
    if utype == "farmer": return redirect("/landing")
    if utype == "business": return redirect("/landingb")
    return redirect("/citizen")

# ROLE-BASED PRODUCT LISTINGS
@app.route("/landing") # Farmer Landing
def landing():
    db_local = ensure_db_connection()
    products = list(db_local.products.find({"owner": session.get("user")}))
    for p in products:
        p['_id'] = str(p['_id'])
        q = p.get('quality_score', 0.8)
        p['quality_label'] = p.get('user_quality') or get_quality_label(q)
        base_rating = 3.5 + (max(0, q-0.5) / 0.5) * 1.5
        p['rating'] = round(min(5.0, base_rating * (1 + random.uniform(0.05, 0.10))), 1)
    return render_template("landing.html", products=products)

@app.route("/landingb") # Business Landing/Control Panel
def landingb():
    db_local = ensure_db_connection()
    products = list(db_local.products.find())
    for p in products:
        p['_id'] = str(p['_id'])
        q = p.get('quality_score', 0.8)
        p['quality_label'] = p.get('user_quality') or get_quality_label(q)
        base_rating = 3.5 + (max(0, q-0.5) / 0.5) * 1.5
        p['rating'] = round(min(5.0, base_rating * (1 + random.uniform(0.05, 0.10))), 1)
    return render_template("landingb.html", products=products)

@app.route("/citizen") # Citizen Landing
def citizen():
    db_local = ensure_db_connection()
    products = list(db_local.products.find().sort("created_at", -1))
    for p in products:
        p['_id'] = str(p['_id'])
        q = p.get('quality_score', 0.8)
        p['quality_label'] = p.get('user_quality') or get_quality_label(q)
        base_rating = 3.5 + (max(0, q-0.5) / 0.5) * 1.5
        p['rating'] = round(min(5.0, base_rating * (1 + random.uniform(0.05, 0.10))), 1)
    return render_template("index.html", products=products)

@app.route("/add-listing")
def add_listing_page():
    if "user" not in session: return redirect("/login")
    if session.get("user_type") not in ["farmer", "business", "admin"]:
        flash("Unauthorized access!", "error")
        return redirect("/dashboard")
    return render_template("add_product.html")

@app.route("/add_product", methods=["POST"])
def add_product():
    if "user" not in session: 
        return redirect("/login")
    
    if session.get("user_type") not in ["farmer", "business", "admin"]:
        flash("Only farmers and businesses can list products!", "error")
        return redirect("/dashboard")
    
    try:
        db_local = ensure_db_connection()
        if db_local is None:
            flash("Database connection error. Please try again later.", "error")
            return redirect("/dashboard")

        name = request.form.get("name")
        price_str = request.form.get("price", "0")
        category = request.form.get("category", "General")
        desc = request.form.get("description", "")
        
        print(f"DEBUG: Received product upload for '{name}' with price {price_str}")

        # Validate name
        if not name:
            flash("Product name is required.", "error")
            return redirect("/add-listing")

        # Validate price
        try:
            price = float(price_str)
        except (ValueError, TypeError):
            flash("Invalid price provided. Please enter a numeric value.", "error")
            return redirect("/add-listing")

        # Handle image upload
        file = request.files.get("image")
        image_url = ""
        if file and file.filename:
            try:
                original_fname = secure_filename(file.filename)
                if not original_fname:
                    original_fname = f"product_{int(datetime.now().timestamp())}.png"
                
                fname = f"{int(datetime.now().timestamp())}_{original_fname}"
                image_dir = os.path.join(os.path.dirname(__file__), 'statics', 'image')
                
                # Try local saving first (works on local dev)
                try:
                    os.makedirs(image_dir, exist_ok=True)
                    path = os.path.join(image_dir, fname)
                    file.save(path)
                    image_url = f"/statics/image/{fname}"
                except (OSError, IOError) as e:
                    # If read-only (Vercel), convert to Base64
                    import base64
                    file.seek(0)
                    file_data = file.read()
                    base64_data = base64.b64encode(file_data).decode('utf-8')
                    mime_type = file.content_type or 'image/jpeg'
                    image_url = f"data:{mime_type};base64,{base64_data}"
                    print("Environment is Read-Only. Stored image as Base64 in Database.")
            except Exception as img_err:
                print(f"Image processing error: {img_err}")
                # Continue without image if it fails
                image_url = ""

        address = request.form.get("address", "")
        user_quality = request.form.get("user_quality", "")
        quantity = int(request.form.get("quantity", 0))

        quality = analyze_quality(name, desc, category, price)
        adj_price = compute_adjusted_price(price, quality)

        db_local.products.insert_one({
            "name": name, 
            "price": price, 
            "adjusted_price": adj_price,
            "category": category, 
            "description": desc, 
            "address": address,
            "image": image_url,
            "owner": session.get("user"), 
            "owner_type": session.get("user_type"),
            "quality_score": quality,
            "user_quality": user_quality, # Store user specified quality
            "quantity": quantity,
            "created_at": datetime.now()
        })
        flash("Product listed successfully with AI Quality Score!", "success")
        return redirect("/dashboard")
        
    except Exception as e:
        print(f"CRITICAL Error in add_product: {str(e)}")
        traceback.print_exc()
        flash(f"Upload Error: {str(e)}", "error")
        return redirect("/add-listing")

# Context Processor
@app.context_processor
def inject_razorpay_key():
    return dict(RAZORPAY_KEY_ID=RAZORPAY_KEY_ID)

@app.route("/checkout/<product_id>")
def checkout(product_id):
    if "user" not in session: return redirect("/login")
    try:
        db_local = ensure_db_connection()
        if db_local is None:
            flash("Database connection lost. Please refresh.", "error")
            return redirect("/dashboard")

        # Robust ID cleaning
        raw_id = str(product_id).strip()
        if "ObjectId('" in raw_id:
            raw_id = raw_id.replace("ObjectId('", "").replace("')", "")
        
        print(f"DEBUG: Checkout requested for Product ID: {raw_id}")
        
        try:
            obj_id = ObjectId(raw_id)
        except Exception:
            print(f"ERROR: Invalid ObjectId format: {raw_id}")
            flash("Invalid link format. Please go back to Marketplace.", "error")
            return redirect("/citizen")

        product = db_local.products.find_one({"_id": obj_id})
        
        if not product:
            print(f"ERROR: Product not found for ID: {raw_id}")
            flash("This product is no longer available.", "error")
            return redirect("/citizen")
        
        # Ensure all required fields exist for template
        product['_id'] = str(product['_id'])
        if 'adjusted_price' not in product:
            product['adjusted_price'] = product.get('price', 0)
            
        user = db_local.users.find_one({"email": session.get("user")})
        return render_template("checkout.html", product=product, user=user)
        
    except Exception as e:
        print(f"CRITICAL Checkout error: {str(e)}")
        traceback.print_exc()
        flash("System error loading checkout. Please try again.", "error")
        return redirect("/dashboard")

# PAYMENT INTEGRATION
@app.route("/create_order", methods=["POST"])
def create_order():
    if not razorpay_client:
        return jsonify({"error": "Razorpay client not configured"}), 500
    try:
        data = request.get_json()
        raw_amount = data.get('amount', 0)
        
        # Razorpay expects amount in paise (integer)
        amount = int(float(raw_amount) * 100)
        
        if amount <= 0:
            return jsonify({"error": "Invalid amount"}), 400
            
        print(f"Creating Razorpay order for {amount} paise...")
        
        order_params = {
            "amount": amount,
            "currency": "INR",
            "payment_capture": 1 # Auto-capture payment
        }
        
        order = razorpay_client.order.create(order_params)
        
        print(f"Order created successfully: {order.get('id')}")
        return jsonify(order)
    except Exception as e:
        print(f"Error creating order: {str(e)}")
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500

@app.route("/verify_payment", methods=["POST"])
def verify_payment():
    try:
        data = request.get_json()
        if not data:
            return jsonify({"status": "failed", "message": "No data received"}), 400
        
        # Verify payment signature
        # Use the global client which is initialized with the correct secret
        params_dict = {
            'razorpay_order_id': data.get('razorpay_order_id'),
            'razorpay_payment_id': data.get('razorpay_payment_id'),
            'razorpay_signature': data.get('razorpay_signature')
        }
        
        # Explicit check to ensure client exists
        if not razorpay_client:
            raise Exception("Razorpay Client not initialized on server")
            
        razorpay_client.utility.verify_payment_signature(params_dict)

        db_local = ensure_db_connection()
        product = db_local.products.find_one({"_id": ObjectId(data.get('product_id'))})
        
        # Extract dynamic delivery info
        qty = int(data.get('quantity', 1))
        delivery_address = data.get('address', "KropKart Hub")
        pincode = data.get('pincode', "110001")
        
        # Create Shiprocket Order
        shipment_id = None
        if shiprocket_token and product:
            try:
                # Optimized Shiprocket Order Payload
                sr_order = {
                    "order_id": f"{data.get('razorpay_order_id')}_{random.randint(100,999)}", # Unique order ID for SR
                    "order_date": datetime.now().strftime("%Y-%m-%d %H:%M"),
                    "pickup_location": "Primary", 
                    "billing_customer_name": session.get("name", "Customer"),
                    "billing_last_name": " ",
                    "billing_address": delivery_address,
                    "billing_city": "Delivery City", # Ideally parsed from address or user profile
                    "billing_pincode": pincode,
                    "billing_state": "Delivery State",
                    "billing_country": "India",
                    "billing_email": session.get("user", "email@example.com"),
                    "billing_phone": "9999999999", 
                    "shipping_is_billing": True,
                    "order_items": [
                        {
                            "name": product.get("name"),
                            "sku": str(product.get("_id")),
                            "units": qty,
                            "selling_price": float(product.get("adjusted_price", 0)),
                            "discount": 0,
                            "tax": 0,
                            "hsn": 441122
                        }
                    ],
                    "payment_method": "Prepaid",
                    "shipping_charges": 0,
                    "gift_wrap_charges": 0,
                    "transaction_charges": 0,
                    "total_discount": 0,
                    "sub_total": data.get('amount'),
                    "length": 10,
                    "breadth": 10,
                    "height": 10,
                    "weight": 1 * qty
                }
                
                sr_res = requests.post(
                    "https://apiv2.shiprocket.in/v1/external/orders/create/adhoc",
                    json=sr_order,
                    headers={"Authorization": f"Bearer {shiprocket_token}"}
                )
                
                if sr_res.status_code == 200:
                    sr_data = sr_res.json()
                    shipment_id = sr_data.get('shipment_id')
                    print(f"✅ Shiprocket Shipment Created: {shipment_id}")
                else:
                    print(f"⚠️ Shiprocket Order Failed: {sr_res.text}")
                    
            except Exception as sr_e:
                print(f"Shiprocket Integration Error: {sr_e}")
        
        # Fetch payment details to get payment method (UPI, Card, etc.)
        payment_method = "Razorpay"
        try:
            payment_details = razorpay_client.payment.fetch(data.get('razorpay_payment_id'))
            payment_method = payment_details.get('method', 'Razorpay').upper()
        except Exception:
            pass

        # Record order in DB with full delivery details
        db_local.orders.insert_one({
            "user": session.get("user"), 
            "product_id": data.get('product_id'),
            "product_name": product.get("name") if product else "Unknown",
            "quantity": qty,
            "amount": data.get('amount'), 
            "payment_method": payment_method, # New field
            "delivery_address": delivery_address,
            "pincode": pincode,
            "status": "paid",
            "delivery_status": "Processing",
            "shipment_id": shipment_id, 
            "razorpay_payment_id": data.get('razorpay_payment_id'),
            "date": datetime.now()
        })
        
        # DECREMENT PRODUCT QUANTITY
        if product:
            new_qty = max(0, product.get("quantity", 0) - qty)
            db_local.products.update_one(
                {"_id": ObjectId(data.get('product_id'))},
                {"$set": {"quantity": new_qty}}
            )
            print(f"DEBUG: Updated product {product.get('name')} quantity to {new_qty}")
        
        print(f"Payment verified: {data.get('razorpay_payment_id')}")
        return jsonify({"status": "success", "shipment_id": shipment_id})
    except razorpay.errors.SignatureVerificationError as e:
        print(f"Signature Verification Failed details: {str(e)}")
        # Log the received signature for debugging (be careful with logs in production)
        print(f"Received Signature: {data.get('razorpay_signature')}")
        print(f"Expected Order ID: {data.get('razorpay_order_id')}")
        return jsonify({"status": "failed", "message": "Security Check Failed: Payment Signature Mismatch"}), 400
    except Exception as e:
        print(f"Payment verification error: {str(e)}")
        return jsonify({"status": "failed", "message": str(e)}), 500

# Webhook Setup
RAZORPAY_WEBHOOK_SECRET = os.getenv("RAZORPAY_WEBHOOK_SECRET", "Saikiran9493@#")

@app.route("/webhook", methods=["POST"])
def webhook():
    # Verify the signature
    signature = request.headers.get('X-Razorpay-Signature')
    body = request.get_data().decode('utf-8')

    try:
        razorpay_client.utility.verify_webhook_signature(body, signature, RAZORPAY_WEBHOOK_SECRET)
        
        # Process the event
        event = request.json
        if event['event'] == 'payment.captured':
            payment = event['payload']['payment']['entity']
            # Here you can update order status in DB if needed (server-to-server confirmation)
            # db_local.orders.update_one(...)
            print(f"Payment Captured: {payment['id']}")
            
        return jsonify({"status": "ok"}), 200
    except razorpay.errors.SignatureVerificationError:
        return jsonify({"status": "error", "message": "Invalid Signature"}), 400
    except Exception as e:
        print(e)
        return jsonify({"status": "error"}), 500

# AI CHATBOT API
@app.route("/api/chat", methods=["POST"])
def chat():
    msg = request.json.get("message", "").lower()
    
    # Enhanced Knowledge Base
    responses = {
        "price": "Current market prices (per quintal):<br>• Rice: ₹2,200<br>• Wheat: ₹2,125<br>• Cotton: ₹6,080<br>Check the 'Live Market' section for more.",
        "paddy": "Paddy (Rice) is currently trending at ₹2,200/quintal. Best time to sell is late November.",
        "wheat": "Wheat prices are stable at ₹2,125. Demand is high in North India.",
        "organic": "Organic certification can increase your produce value by 20-30%. We prioritize organic listings!",
        "buy": "<b>To Buy:</b><br>1. Go to Marketplace<br>2. Select products<br>3. Click 'Buy Now' to pay securely via Razorpay.",
        "sell": "<b>To Sell:</b><br>1. Login as Farmer/Business<br>2. Go to Dashboard<br>3. Click 'List Product' or use the 'List Inventory' button.",
        "quality": "Our AI Quality Score considers:<br>• Visual freshness (via image)<br>• Product description<br>• Standard grade specifications.",
        "subsidy": "Govt subsidies available for:<br>• Drip Irrigation (50%)<br>• Solar Pumps (pm-KUSUM)<br>• Organic Fertilizer.",
        "weather": "It looks sunny across major farming belts. Good for harvesting! (Real-time weather integration coming soon).",
        "pest": "For pests, we recommend organic neem oil spray initially. For severe infestations, consult an agronome.",
        "hello": "Namaste! 🙏 I am KropBot. How can I help you with your farming journey today?",
        "hi": "Hello there! ready to help you with crops, prices, or navigating KropKart.",
        "kropkart": "KropKart is an AI-powered marketplace connecting farmers directly to buyers, ensuring fair prices and fresh produce.",
        "loan": "KropKart partners with banks to offer Kisan Credit Cards. Check the 'Finance' section in your dashboard."
    }
    
    # Fuzzy matching logic
    best_response = None
    for key in responses:
        if key in msg:
            best_response = responses[key]
            break
            
    if not best_response:
        # Fallback for common agriculture terms not explicitly caught
        if any(x in msg for x in ["corn", "maize", "dal", "pulses", "gram"]):
            best_response = "We have listings for that crop! Please check the <a href='/citizen'>Marketplace</a> for live availability."
        elif any(x in msg for x in ["login", "signin", "account"]):
            best_response = "You can <a href='/login'>Login here</a>. If you don't have an account, please <a href='/register'>Register</a>."
        else:
            best_response = "I'm not sure about that specific query. Try asking about:<br>• Crop Prices (Rice, Wheat)<br>• Buying/Selling<br>• Organic Farming<br>• Government Schemes"
            
    return jsonify({"response": best_response})

@app.route("/admin/refund_order/<order_id>")
def refund_order(order_id):
    if "user" not in session: return redirect("/login")
    if session.get("user_type") != "admin":
        flash("Unauthorized access!", "error")
        return redirect("/dashboard")
        
    try:
        db_local = ensure_db_connection()
        # Clean ID
        clean_id = order_id.replace("ObjectId('", "").replace("')", "").strip()
        order = db_local.orders.find_one({"_id": ObjectId(clean_id)})
        
        if not order:
            flash("Order not found.", "error")
            return redirect("/admin")
            
        if order.get("status") == "Refunded":
            flash("Order is already refunded.", "info")
            return redirect("/admin")
            
        payment_id = order.get("razorpay_payment_id")
        if not payment_id:
            flash("No payment ID found for this order. Cannot refund.", "error")
            return redirect("/admin")
            
        print(f"Initiating refund for Payment ID: {payment_id}")
        
        if not razorpay_client:
            flash("Razorpay client not ready.", "error")
            return redirect("/admin")
            
        # Execute Refund via Razorpay
        # amount is in paise, same as stored in DB
        refund_res = razorpay_client.payment.refund(payment_id, {
            "amount": int(order.get("amount", 0)),
            "speed": "normal",
            "notes": {
                "reason": "Admin initiated refund via KropKart Dashboard"
            }
        })
        
        # Update DB
        db_local.orders.update_one(
            {"_id": ObjectId(clean_id)},
            {"$set": {
                "status": "Refunded",
                "refund_id": refund_res.get("id"),
                "refunded_at": datetime.now()
            }}
        )
        
        flash(f"Refund of ₹{int(order.get('amount',0))/100} processed successfully!", "success")
        
    except Exception as e:
        print(f"Refund Error: {e}")
        traceback.print_exc()
        flash(f"Refund Failed: {str(e)}", "error")
        
    return redirect("/admin")

@app.route("/admin")
def admin():
    if "user" not in session: return redirect("/login")
    if session.get("user_type") != "admin":
        flash("Unauthorized access!", "error")
        return redirect("/dashboard")
    
    db_local = ensure_db_connection()
    
    # Fetch Data
    users_list = list(db_local.users.find())
    products_list = list(db_local.products.find())
    orders_list = list(db_local.orders.find().sort("date", -1))
    
    # Calculate Stats
    total_users = len(users_list)
    total_products = len(products_list)
    total_orders = len(orders_list)
    total_revenue = sum(float(order.get('amount', 0)) for order in orders_list)
    
    user_counts = {}
    for u in users_list:
        rtype = u.get("user_type", "unknown")
        user_counts[rtype] = user_counts.get(rtype, 0) + 1
        
    return render_template("admin.html", 
                           total_revenue=total_revenue, 
                           total_users=total_users, 
                           total_products=total_products,
                           total_orders=total_orders,
                           recent_orders=orders_list[:50],
                           user_counts=user_counts)

@app.route("/profile", methods=["GET", "POST"])
def profile():
    if "user" not in session: return redirect("/login")
    if session.get("user_type") == "admin":
        flash("Admins do not have a profile page.", "info")
        return redirect("/dashboard")
    
    db_local = ensure_db_connection()
    user = db_local.users.find_one({"email": session.get("user")})
    
    # AUTO-GENERATION FOR EXISTING USERS
    if not user.get("user_id"):
        utype = user.get("user_type")
        new_id = None
        if utype == "farmer":
            new_id = f"FRM-{random.randint(100000, 999999)}"
        elif utype == "business":
            new_id = f"BUS-{random.randint(100000, 999999)}"
        
        if new_id:
            db_local.users.update_one({"_id": user["_id"]}, {"$set": {"user_id": new_id}})
            user["user_id"] = new_id
            session["user_id"] = new_id
    
    if request.method == "POST":
        # Update details
        name = request.form.get("name")
        phone = request.form.get("phone")
        address = request.form.get("address")
        
        # Bank Details
        bank_name = request.form.get("bank_name")
        account_number = request.form.get("account_number")
        ifsc_code = request.form.get("ifsc_code")
        upi_id = request.form.get("upi_id")
        
        # Additional Payment Methods (as a comma-separated string or list)
        payment_methods = request.form.get("payment_methods", "")
        
        update_data = {
            "name": name,
            "phone": phone,
            "address": address,
            "bank_details": {
                "bank_name": bank_name,
                "account_number": account_number,
                "ifsc_code": ifsc_code,
                "upi_id": upi_id
            },
            "payment_methods": payment_methods
        }
        
        db_local.users.update_one(
            {"email": session.get("user")},
            {"$set": update_data}
        )
        
        session["name"] = name  # Update name in session if changed
        flash("Profile updated successfully!", "success")
        return redirect("/profile")
        
    return render_template("profile.html", user=user)

@app.route("/my-orders")
def my_orders():
    if "user" not in session: return redirect("/login")
    db_local = ensure_db_connection()
    orders_list = list(db_local.orders.find({"user": session.get("user")}).sort("date", -1))
    return render_template("my_orders.html", orders=orders_list)

@app.route("/logout")
def logout():
    session.clear()
    flash("Successfully logged out!", "success")
    return redirect("/")

@app.route("/delete_product/<product_id>")
def delete_product(product_id):
    if "user" not in session: return redirect("/login")
    try:
        db_local = ensure_db_connection()
        clean_id = product_id.replace("ObjectId('", "").replace("')", "").strip()
        product = db_local.products.find_one({"_id": ObjectId(clean_id)})
        
        if not product:
            flash("Product not found!", "error")
            return redirect("/")
        
        # Check if user is admin or owner
        if session.get("user_type") == "admin" or product.get("owner") == session.get("user"):
            db_local.products.delete_one({"_id": ObjectId(clean_id)})
            flash("Product deleted successfully!", "success")
        else:
            flash("Unauthorized to delete this product!", "error")
    except Exception as e:
        print(f"Delete Error: {e}")
        flash("Invalid product reference.", "error")
        
    return redirect(request.referrer or "/")

@app.route("/quality-analysis")
def quality_analysis():
    if "user" not in session: return redirect("/login")
    return render_template("quality_analysis.html")

@app.route("/run-analysis", methods=["POST"])
def run_analysis():
    if "user" not in session: return jsonify({"error": "Unauthorized"}), 401
    
    # Mocking the AI analysis process
    score = random.randint(85, 98)
    results = {
        "freshness": f"{score}%",
        "ripeness": "Optimal" if score > 90 else "Good",
        "defects": "None Detected" if score > 92 else "Minor surface marks",
        "quality_score": score,
        "grade": "Grade A+" if score > 95 else "Grade A",
        "market_valuation": f"₹{random.randint(2000, 2500)} / Quintal"
    }
    return jsonify(results)

if __name__ == "__main__":
    init_db()
    # Triggering reload for env update v2
    app.run(debug=True, port=int(os.getenv("PORT", 5000)))
