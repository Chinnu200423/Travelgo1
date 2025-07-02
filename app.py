from flask import Flask, render_template, request, redirect, url_for, session, jsonify, flash
import boto3
from boto3.dynamodb.conditions import Key, Attr
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime
from decimal import Decimal
import uuid
import random

app = Flask(__name__)
app.secret_key = 'your_secret_key_here' # IMPORTANT: Change this to a strong, random key in production

# AWS Setup using IAM Role
REGION = 'ap-south-1'  # Replace with your actual AWS region
dynamodb = boto3.resource('dynamodb', region_name=REGION)
sns_client = boto3.client('sns', region_name=REGION)

# Ensure these table names match your DynamoDB tables
users_table = dynamodb.Table('travelgo_users')
trains_table = dynamodb.Table('trains') # This table is referenced but not used for data retrieval in the provided code.
bookings_table = dynamodb.Table('bookings')

# Replace with your actual SNS topic ARN
SNS_TOPIC_ARN = 'arn:aws:sns:ap-south-1:353250843450:TravelGoBookingTopic'

def send_sns_notification(subject, message):
    """
    Sends an SNS notification to the configured topic.
    """
    try:
        sns_client.publish(
            TopicArn=SNS_TOPIC_ARN,
            Subject=subject,
            Message=message
        )
        print(f"SNS notification sent: Subject='{subject}', Message='{message}'")
    except Exception as e:
        print(f"SNS Error: {e}")

# Routes
@app.route('/')
def index():
    """
    Renders the homepage.
    """
    return render_template('index.html')

@app.route('/register', methods=['GET', 'POST'])
def register():
    """
    Handles user registration.
    - GET: Displays the registration form.
    - POST: Processes registration, hashes password, and stores user in DynamoDB.
    """
    if request.method == 'POST':
        email = request.form['email']
        password = request.form['password']
        
        # Check if email already exists
        existing = users_table.get_item(Key={'email': email})
        if 'Item' in existing:
            flash('Email already exists!', 'error')
            return render_template('register.html')
        
        # Hash the password before storing
        hashed_password = generate_password_hash(password)
        
        # Store user in DynamoDB
        users_table.put_item(Item={'email': email, 'password': hashed_password})
        
        flash('Registration successful! Please log in.', 'success')
        return redirect(url_for('login'))
    return render_template('register.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    """
    Handles user login.
    - GET: Displays the login form.
    - POST: Authenticates user against DynamoDB and sets session.
    """
    if request.method == 'POST':
        email = request.form['email']
        password = request.form['password']
        
        user = users_table.get_item(Key={'email': email})
        
        # Check if user exists and password is correct
        if 'Item' in user and check_password_hash(user['Item']['password'], password):
            session['email'] = email
            flash('Logged in successfully!', 'success')
            return redirect(url_for('dashboard'))
        else:
            flash('Invalid email or password!', 'error')
            return render_template('login.html')
    return render_template('login.html')

@app.route('/logout')
def logout():
    """
    Logs out the current user by clearing the session.
    """
    session.pop('email', None)
    flash('You have been logged out.', 'info')
    return redirect(url_for('index'))

@app.route('/dashboard')
def dashboard():
    """
    Displays the user's dashboard, showing their past bookings.
    Requires user to be logged in.
    """
    if 'email' not in session:
        return redirect(url_for('login'))
    
    user_email = session['email']
    
    # Query bookings for the logged-in user
    response = bookings_table.query(
        KeyConditionExpression=Key('user_email').eq(user_email),
        ScanIndexForward=False # Get most recent bookings first
    )
    bookings = response.get('Items', [])
    
    # Convert Decimal types to float for rendering in HTML
    for booking in bookings:
        if 'total_price' in booking:
            try:
                booking['total_price'] = float(booking['total_price'])
            except Exception:
                booking['total_price'] = 0.0 # Default if conversion fails
    
    return render_template('dashboard.html', username=user_email, bookings=bookings)

@app.route('/train')
def train():
    """
    Displays the train booking search page.
    Requires user to be logged in.
    """
    if 'email' not in session:
        return redirect(url_for('login'))
    return render_template('train.html')

@app.route('/confirm_train_details')
def confirm_train_details():
    """
    Confirms train booking details and checks for seat availability.
    Requires user to be logged in.
    """
    if 'email' not in session:
        return redirect(url_for('login'))

    # Collect booking details from request arguments
    booking_details = {
        'name': request.args.get('name'),
        'train_number': request.args.get('trainNumber'),
        'source': request.args.get('source'),
        'destination': request.args.get('destination'),
        'departure_time': request.args.get('departureTime'),
        'arrival_time': request.args.get('arrivalTime'),
        'price_per_person': Decimal(request.args.get('price')),
        'travel_date': request.args.get('date'),
        'num_persons': int(request.args.get('persons')),
        'item_id': request.args.get('trainId'),
        'booking_type': 'train',
        'user_email': session['email'],
        'total_price': Decimal(request.args.get('price')) * int(request.args.get('persons'))
    }

    # Query existing bookings for the specific train and date to determine available seats
    response = bookings_table.query(
        IndexName='GSI_ItemDate', # Ensure this GSI exists on 'item_id' and 'travel_date'
        KeyConditionExpression=Key('item_id').eq(booking_details['item_id']) & Key('travel_date').eq(booking_details['travel_date'])
    )

    booked_seats = set()
    for b in response.get('Items', []):
        if 'seats_display' in b:
            booked_seats.update(b['seats_display'].split(', '))

    all_seats = [f"S{i}" for i in range(1, 101)] # Assuming 100 seats per train
    available_seats = [seat for seat in all_seats if seat not in booked_seats]

    # Check if enough seats are available
    if len(available_seats) < booking_details['num_persons']:
        flash("Not enough seats available for the selected number of persons.", "error")
        return redirect(url_for("train"))

    # Store pending booking details in session
    session['pending_booking'] = booking_details
    
    # Display available seats (randomly pick some for display, actual allocation happens on final confirm)
    seats_to_display = random.sample(available_seats, min(len(available_seats), booking_details['num_persons']))

    return render_template('confirm_train_details.html', booking=booking_details, available_seats=seats_to_display)

@app.route('/final_confirm_train_booking', methods=['POST'])
def final_confirm_train_booking():
    """
    Finalizes the train booking after seat selection.
    Allocates seats and stores the booking in DynamoDB.
    Sends SNS notification.
    """
    if 'email' not in session:
        return jsonify({'success': False, 'message': 'User not logged in'}), 401

    booking_data = session.pop('pending_booking', None)
    if not booking_data:
        return jsonify({'success': False, 'message': 'No pending booking found'}), 400

    # Re-check seat availability to prevent race conditions
    response = bookings_table.query(
        IndexName='GSI_ItemDate',
        KeyConditionExpression=Key('item_id').eq(booking_data['item_id']) & Key('travel_date').eq(booking_data['travel_date'])
    )

    booked_seats = set()
    for b in response.get('Items', []):
        if 'seats_display' in b:
            booked_seats.update(b['seats_display'].split(', '))

    all_seats = [f"S{i}" for i in range(1, 101)]
    available_seats = [seat for seat in all_seats if seat not in booked_seats]

    if len(available_seats) < booking_data['num_persons']:
        return jsonify({'success': False, 'message': 'Not enough seats available. Please try again.'}), 400

    # Allocate seats randomly from available ones
    allocated_seats = random.sample(available_seats, booking_data['num_persons'])
    booking_data['seats_display'] = ', '.join(allocated_seats)
    
    # Generate unique booking ID and set booking date
    booking_data['booking_id'] = str(uuid.uuid4())
    booking_data['booking_date'] = datetime.now().isoformat()

    # Store the confirmed booking in DynamoDB
    bookings_table.put_item(Item=booking_data)

    # Send SNS notification
    send_sns_notification(
        subject="Train Booking Confirmed",
        message=f"Train {booking_data['train_number']} from {booking_data['source']} to {booking_data['destination']} on {booking_data['travel_date']} is confirmed.\nSeats: {booking_data['seats_display']}\nTotal: ₹{booking_data['total_price']}"
    )

    return jsonify({'success': True, 'message': 'Train booking confirmed successfully!', 'redirect': url_for('dashboard')})

@app.route('/bus')
def bus():
    """
    Displays the bus booking search page.
    Requires user to be logged in.
    """
    if 'email' not in session:
        return redirect(url_for('login'))
    return render_template('bus.html')

@app.route('/confirm_bus_details')
def confirm_bus_details():
    """
    Confirms bus booking details before seat selection.
    Stores details in session for later use.
    """
    if 'email' not in session:
        return redirect(url_for('login'))

    booking_details = {
        'name': request.args.get('name'),
        'source': request.args.get('source'),
        'destination': request.args.get('destination'),
        'time': request.args.get('time'),
        'type': request.args.get('type'),
        'price_per_person': Decimal(request.args.get('price')),
        'travel_date': request.args.get('date'),
        'num_persons': int(request.args.get('persons')),
        'item_id': request.args.get('busId'),
        'booking_type': 'bus',
        'user_email': session['email'],
        'total_price': Decimal(request.args.get('price')) * int(request.args.get('persons'))
    }
    session['pending_booking'] = booking_details
    return render_template('confirm_bus_details.html', booking=booking_details)

@app.route('/select_bus_seats')
def select_bus_seats():
    """
    Displays the bus seat selection page.
    Shows already booked seats for the selected bus and date.
    """
    if 'email' not in session:
        return redirect(url_for('login'))

    # Reconstruct booking details from query args (or session if available)
    booking = {
        'name': request.args.get('name'),
        'source': request.args.get('source'),
        'destination': request.args.get('destination'),
        'time': request.args.get('time'),
        'type': request.args.get('type'),
        'price_per_person': Decimal(request.args.get('price')),
        'travel_date': request.args.get('date'),
        'num_persons': int(request.args.get('persons')),
        'item_id': request.args.get('busId'),
        'booking_type': 'bus',
        'user_email': session['email'],
        'total_price': Decimal(request.args.get('price')) * int(request.args.get('persons'))
    }

    # Get booked seats for the specific bus and date
    response = bookings_table.query(
        IndexName='GSI_ItemDate', # Ensure this GSI exists
        KeyConditionExpression=Key('item_id').eq(booking['item_id']) & Key('travel_date').eq(booking['travel_date'])
    )

    booked_seats = set()
    for b in response.get('Items', []):
        if 'seats_display' in b:
            booked_seats.update(b['seats_display'].split(', '))

    all_seats = [f"S{i}" for i in range(1, 41)] # Assuming 40 seats per bus
    session['pending_booking'] = booking # Store for final confirmation

    return render_template("select_bus_seats.html", booking=booking, booked_seats=booked_seats, all_seats=all_seats)

@app.route('/final_confirm_bus_booking', methods=['POST'])
def final_confirm_bus_booking():
    """
    Finalizes the bus booking with selected seats.
    Stores the booking in DynamoDB and sends SNS notification.
    """
    if 'email' not in session:
        return redirect(url_for('login'))

    booking = session.pop('pending_booking', None)
    selected_seats_str = request.form.get('selected_seats') # Get selected seats from form

    if not booking or not selected_seats_str:
        flash("Booking failed! Missing data.", "error")
        return redirect(url_for("bus"))

    selected_seats = selected_seats_str.split(', ')
    
    # Re-check for double booking to prevent race conditions
    response = bookings_table.query(
        IndexName='GSI_ItemDate',
        KeyConditionExpression=Key('item_id').eq(booking['item_id']) & Key('travel_date').eq(booking['travel_date'])
    )
    existing_booked_seats = set()
    for b in response.get('Items', []):
        if 'seats_display' in b:
            existing_booked_seats.update(b['seats_display'].split(', '))

    # Check if any selected seat is already booked
    if any(s in existing_booked_seats for s in selected_seats):
        flash("One or more selected seats are already booked! Please select different seats.", "error")
        return redirect(url_for("bus"))

    # Update booking with selected seats, ID, and date
    booking['seats_display'] = selected_seats_str
    booking['booking_id'] = str(uuid.uuid4())
    booking['booking_date'] = datetime.now().isoformat()

    # Store the confirmed booking
    bookings_table.put_item(Item=booking)
    
    # Send SNS notification
    send_sns_notification(
        subject="Bus Booking Confirmed",
        message=f"Your bus from {booking['source']} to {booking['destination']} on {booking['travel_date']} is confirmed.\nSeats: {booking['seats_display']}\nTotal: ₹{booking['total_price']}"
    )

    flash('Bus booking confirmed!', 'success')
    return redirect(url_for('dashboard'))

@app.route('/flight')
def flight():
    """
    Displays the flight booking search page.
    Requires user to be logged in.
    """
    if 'email' not in session:
        return redirect(url_for('login'))
    return render_template('flight.html')

@app.route('/confirm_flight_details')
def confirm_flight_details():
    """
    Confirms flight booking details before final confirmation.
    """
    if 'email' not in session:
        return redirect(url_for('login'))

    # Collect flight details from query arguments
    booking = {
        'flight_id': request.args['flight_id'],
        'airline': request.args['airline'],
        'flight_number': request.args['flight_number'],
        'source': request.args['source'],
        'destination': request.args['destination'],
        'departure_time': request.args['departure'],
        'arrival_time': request.args['arrival'],
        'travel_date': request.args['date'],
        'num_persons': int(request.args['passengers']),
        'price_per_person': float(request.args['price']),
    }
    booking['total_price'] = booking['price_per_person'] * booking['num_persons']
    
    # Store pending booking in session
    session['pending_flight_booking'] = booking # Using a different key to avoid conflict

    return render_template('confirm_flight_details.html', booking=booking)

@app.route('/confirm_flight_booking', methods=['POST'])
def confirm_flight_booking():
    """
    Finalizes the flight booking.
    Stores the booking in DynamoDB and sends SNS notification.
    """
    if 'email' not in session:
        return redirect(url_for('login'))

    # Retrieve booking from session
    booking = session.pop('pending_flight_booking', None)

    if not booking:
        flash("Flight booking failed! No pending booking found.", "error")
        return redirect(url_for('flight'))

    # Add additional booking details
    booking['booking_type'] = 'flight'
    booking['user_email'] = session['email']
    booking['booking_date'] = datetime.now().isoformat()
    booking['booking_id'] = str(uuid.uuid4())
    
    # Ensure Decimal types for DynamoDB
    booking['price_per_person'] = Decimal(str(booking['price_per_person']))
    booking['total_price'] = Decimal(str(booking['total_price']))

    # Store the confirmed booking
    bookings_table.put_item(Item=booking)

    # Send SNS notification
    send_sns_notification(
        subject="Flight Booking Confirmed",
        message=f"Your flight booking on {booking['travel_date']} from {booking['source']} to {booking['destination']} with {booking['airline']} is confirmed.\nTotal: ₹{booking['total_price']}"
    )

    flash('Flight booking confirmed successfully!', 'success')
    return redirect(url_for('dashboard'))

@app.route('/hotel')
def hotel():
    """
    Displays the hotel booking search page.
    Requires user to be logged in.
    """
    if 'email' not in session:
        return redirect(url_for('login'))
    return render_template('hotel.html')

@app.route('/confirm_hotel_details')
def confirm_hotel_details():
    """
    Confirms hotel booking details before final confirmation.
    Calculates total price based on nights.
    """
    if 'email' not in session:
        return redirect(url_for('login'))

    # Collect hotel details from query arguments
    booking = {
        'name': request.args.get('name'),
        'location': request.args.get('location'),
        'checkin_date': request.args.get('checkin'),
        'checkout_date': request.args.get('checkout'),
        'num_rooms': int(request.args.get('rooms')),
        'num_guests': int(request.args.get('guests')),
        'price_per_night': Decimal(request.args.get('price')),
        'rating': int(request.args.get('rating'))
    }

    # Calculate number of nights and total price
    ci = datetime.fromisoformat(booking['checkin_date'])
    co = datetime.fromisoformat(booking['checkout_date'])
    nights = (co - ci).days
    booking['nights'] = nights
    booking['total_price'] = booking['price_per_night'] * booking['num_rooms'] * nights
    
    # Store pending booking in session
    session['pending_hotel_booking'] = booking # Using a different key

    return render_template('confirm_hotel_details.html', booking=booking)

@app.route('/confirm_hotel_booking', methods=['POST'])
def confirm_hotel_booking():
    """
    Finalizes the hotel booking.
    Stores the booking in DynamoDB and sends SNS notification.
    """
    if 'email' not in session:
        return redirect(url_for('login'))

    # Retrieve booking from session
    booking = session.pop('pending_hotel_booking', None)

    if not booking:
        flash("Hotel booking failed! No pending booking found.", "error")
        return redirect(url_for('hotel'))

    # Add additional booking details
    booking['booking_type'] = 'hotel'
    booking['user_email'] = session['email']
    booking['booking_date'] = datetime.now().isoformat()
    booking['booking_id'] = str(uuid.uuid4())
    
    # Ensure Decimal types for DynamoDB
    booking['price_per_night'] = Decimal(str(booking['price_per_night']))
    booking['total_price'] = Decimal(str(booking['total_price']))

    # Store the confirmed booking
    bookings_table.put_item(Item=booking)

    # Send SNS notification
    send_sns_notification(
        subject="Hotel Booking Confirmed",
        message=f"Hotel booking at {booking['name']} in {booking['location']} from {booking['checkin_date']} to {booking['checkout_date']} is confirmed.\nTotal: ₹{booking['total_price']}"
    )

    flash('Hotel booking confirmed successfully!', 'success')
    return redirect(url_for('dashboard'))

@app.route('/cancel_booking', methods=['POST'])
def cancel_booking():
    """
    Cancels a user's booking.
    Requires user to be logged in.
    """
    if 'email' not in session:
        return redirect(url_for('login'))

    booking_id = request.form.get('booking_id')
    booking_date = request.form.get('booking_date') # Need booking_date as part of the composite key
    user_email = session['email']

    if not booking_id or not booking_date:
        flash("Error: Booking ID or Booking Date is missing for cancellation.", 'error')
        return redirect(url_for('dashboard'))

    try:
        # Delete item using the composite primary key (user_email, booking_date)
        bookings_table.delete_item(
            Key={'user_email': user_email, 'booking_date': booking_date}
        )
        flash(f"Booking {booking_id} cancelled successfully!", 'success')
    except Exception as e:
        flash(f"Failed to cancel booking: {str(e)}", 'error')

    return redirect(url_for('dashboard'))


if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0')
