1. After cloning the repository, from the root directory
   (recommended to perform this step in a virtual environment):
`pip install -r requirements.txt`
<br>
2. In api/settings.py, for the ALLOWED_HOSTS (in DEV_ENV),
add your local machine's IP address.
<br>
3. To setup a local database, run the following commands 
from the root directory:
`python manage.py makemigrations`
`python manage.py migrate`
<br>
4. To run on your local machine, execute the following command 
from the root directory:<br>
`python manage.py runserver [your IP address]:8000`<br>
ex: `python manage.py runserver 192.168.1.144:8000`