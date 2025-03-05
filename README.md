1. After cloning the repository, from the root directory
   (recommended to perform this step in a virtual environment):
`pip install -r requirements.txt`

2. In api/settings.py, for the ALLOWED_HOSTS (in DEV_ENV),
add your local machine's IP address.

3. To setup a local database, run the following commands 
from the root directory:
`python manage.py makemigrations api`
`python manage.py migrate`

4. Start a local redis server on port 6379.

5. To run on your local machine, execute the following command 
from the root directory:<br>
`python manage.py runserver [your IP address]:8000`<br>
ex: `python manage.py runserver 192.168.1.144:8000`

6. Start Celery from terminal:
`celery -A algo_tracker_backend worker --loglevel=info --pool=solo`

Notes:
* To kill Celery tasks:
`taskkill /F /IM celery.exe`