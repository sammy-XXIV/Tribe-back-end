from flask import Flask, request, jsonify
from flask_cors import CORS
import requests
import threading
import queue
import uuid
import time
import os

app = Flask(__name__)
CORS(app)

TWITTER_API_KEY = os.environ.get('TWITTER_API_KEY', '')
API_BASE = 'https://api.twitterapi.io'

# --- In-memory queue and results store ---
request_queue = queue.Queue()
results = {}       # queue_id -> result or error
positions = {}     # queue_id -> username
queue_order = []   # ordered list of queue_ids (for position tracking)
queue_lock = threading.Lock()

DELAY_BETWEEN_REQUESTS = 2.0  # seconds between API calls

def call_twitter(path, params):
    """Single rate-limited call to twitterapi.io"""
    url = API_BASE + path
    resp = requests.get(url, headers={'x-api-key': TWITTER_API_KEY}, params=params, timeout=15)
    time.sleep(DELAY_BETWEEN_REQUESTS)
    return resp

def find_first_tweet(username):
    """Core logic — fetch user join year, then search that year/month"""
    # Step 1: Get user profile
    r = call_twitter('/twitter/user/info', {'userName': username})
    if r.status_code == 404:
        return None, 'User @{} not found.'.format(username)
    if r.status_code == 429:
        return None, 'Rate limited. Please try again in a minute.'
    if not r.ok:
        return None, 'Error fetching profile: HTTP {}'.format(r.status_code)

    data = r.json()
    u = data.get('data') or data
    if not u or not u.get('createdAt'):
        return None, 'Could not load profile for @{}'.format(username)

    join_date = u['createdAt']
    join_year = int(join_date[:4])
    join_month = int(join_date[5:7])

    base_query = 'from:{} -filter:replies'.format(username)

    # Step 2: Search join year
    year_query = '{} since:{}-01-01 until:{}-01-01'.format(base_query, join_year, join_year + 1)
    r2 = call_twitter('/twitter/tweet/advanced_search', {'query': year_query, 'queryType': 'Latest'})
    if not r2.ok or not r2.json().get('tweets'):
        return None, 'No original posts found for @{}'.format(username)

    # Step 3: Narrow to month starting from join month
    best_tweet = None
    for mo in range(join_month, 13):
        ms = '{}-{:02d}-01'.format(join_year, mo)
        me = '{}-{:02d}-01'.format(join_year, mo + 1) if mo < 12 else '{}-01-01'.format(join_year + 1)
        mq = '{} since:{} until:{}'.format(base_query, ms, me)

        r3 = call_twitter('/twitter/tweet/advanced_search', {'query': mq, 'queryType': 'Latest'})
        if not r3.ok:
            continue
        m_data = r3.json()
        tweets = m_data.get('tweets', [])
        if not tweets:
            continue

        # Collect more pages
        all_tweets = tweets[:]
        cursor = m_data.get('next_cursor') or m_data.get('cursor')
        pages = 0
        while cursor and pages < 5:
            pages += 1
            rp = call_twitter('/twitter/tweet/advanced_search', {'query': mq, 'queryType': 'Latest', 'cursor': cursor})
            if not rp.ok:
                break
            pd = rp.json()
            all_tweets += pd.get('tweets', [])
            cursor = pd.get('next_cursor') or pd.get('cursor')

        all_tweets.sort(key=lambda t: t.get('createdAt', ''))
        if all_tweets:
            best_tweet = all_tweets[0]
            # Inject profile info
            best_tweet['_profile'] = {
                'userName': u.get('userName', username),
                'name': u.get('name', username),
                'profilePicture': u.get('profilePicture', ''),
                'isBlueVerified': u.get('isBlueVerified', False),
                'followers': u.get('followers', 0),
                'following': u.get('following', 0),
                'joinedYear': join_year
            }
            break

    if not best_tweet:
        return None, 'Could not find first post for @{}'.format(username)

    return best_tweet, None


def worker():
    """Background thread — processes one request at a time"""
    while True:
        queue_id = request_queue.get()
        try:
            with queue_lock:
                username = positions.get(queue_id)
                if not username:
                    continue

            results[queue_id] = {'status': 'processing'}

            tweet, error = find_first_tweet(username)

            if error:
                results[queue_id] = {'status': 'error', 'message': error}
            else:
                results[queue_id] = {'status': 'done', 'tweet': tweet}

        except Exception as e:
            results[queue_id] = {'status': 'error', 'message': str(e)}
        finally:
            with queue_lock:
                if queue_id in queue_order:
                    queue_order.remove(queue_id)
            request_queue.task_done()


# Start background worker thread
t = threading.Thread(target=worker, daemon=True)
t.start()

# Self-ping to keep Render free tier awake
def self_ping():
    import urllib.request
    while True:
        time.sleep(600)  # ping every 10 minutes
        try:
            urllib.request.urlopen('https://tribe-back-end.onrender.com/health', timeout=10)
        except:
            pass

ping_thread = threading.Thread(target=self_ping, daemon=True)
ping_thread.start()


@app.route('/queue', methods=['POST'])
def add_to_queue():
    """Add a username search to the queue"""
    data = request.get_json()
    username = (data or {}).get('username', '').strip().lstrip('@')
    if not username:
        return jsonify({'error': 'Missing username'}), 400

    queue_id = str(uuid.uuid4())

    with queue_lock:
        positions[queue_id] = username
        queue_order.append(queue_id)
        position = len(queue_order)

    request_queue.put(queue_id)

    # Estimate: ~10 API calls per user * 2s delay = ~20s per user
    estimated_wait = (position - 1) * 20

    return jsonify({
        'queue_id': queue_id,
        'position': position,
        'estimated_wait': estimated_wait
    })


@app.route('/status/<queue_id>', methods=['GET'])
def get_status(queue_id):
    """Poll for result"""
    with queue_lock:
        position = queue_order.index(queue_id) + 1 if queue_id in queue_order else 0
        estimated_wait = max(0, (position - 1) * 20)

    result = results.get(queue_id)

    if not result:
        return jsonify({'status': 'queued', 'position': position, 'estimated_wait': estimated_wait})

    if result['status'] == 'processing':
        return jsonify({'status': 'processing', 'position': 0, 'estimated_wait': 5})

    if result['status'] == 'error':
        # Clean up
        results.pop(queue_id, None)
        positions.pop(queue_id, None)
        return jsonify({'status': 'error', 'message': result['message']})

    if result['status'] == 'done':
        tweet = result['tweet']
        # Clean up
        results.pop(queue_id, None)
        positions.pop(queue_id, None)
        return jsonify({'status': 'done', 'tweet': tweet})

    return jsonify({'status': 'unknown'})


@app.route('/following', methods=['GET'])
def get_following():
    username = request.args.get('userName', '').strip()
    cursor = request.args.get('cursor', '')
    if not username:
        return jsonify({'error': 'Missing userName'}), 400
    url = '{}/twitter/user/followings?userName={}&pageSize=200'.format(API_BASE, username)
    if cursor:
        url += '&cursor=' + cursor
    try:
        r = requests.get(url, headers={'x-api-key': TWITTER_API_KEY}, timeout=15)
        time.sleep(DELAY_BETWEEN_REQUESTS)
        return jsonify(r.json()), r.status_code
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/followers', methods=['GET'])
def get_followers():
    username = request.args.get('userName', '').strip()
    cursor = request.args.get('cursor', '')
    if not username:
        return jsonify({'error': 'Missing userName'}), 400
    url = '{}/twitter/user/followers?userName={}&pageSize=200'.format(API_BASE, username)
    if cursor:
        url += '&cursor=' + cursor
    try:
        r = requests.get(url, headers={'x-api-key': TWITTER_API_KEY}, timeout=15)
        time.sleep(DELAY_BETWEEN_REQUESTS)
        return jsonify(r.json()), r.status_code
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/health', methods=['GET'])
def health():
    return jsonify({'status': 'ok', 'queue_size': request_queue.qsize()})


if __name__ == '__main__':
    app.run(debug=False, host='0.0.0.0', port=5000)

