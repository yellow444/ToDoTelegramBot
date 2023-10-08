server {
    listen 80;
    listen [::]:80;
    server_name broadband-188-255-24-92.ip.moscow.rt.ru www.broadband-188-255-24-92.ip.moscow.rt.ru;

    location ~ /.well-known/acme-challenge {
        allow all;
        root /var/www/html;
    }
}

server {
    listen 443 ssl;
    server_name broadband-188-255-24-92.ip.moscow.rt.ru www.broadband-188-255-24-92.ip.moscow.rt.ru;

    ssl_certificate /etc/letsencrypt/live/broadband-188-255-24-92.ip.moscow.rt.ru/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/broadband-188-255-24-92.ip.moscow.rt.ru/privkey.pem;

    location / {
        proxy_pass http://172.17.0.1:3000;
    }
}

