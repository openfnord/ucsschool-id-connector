FROM alpine:latest

ARG version

VOLUME /var/log

WORKDIR /ucsschool-id-connector

EXPOSE 8911

CMD ["/sbin/init"]

COPY apline_apk_list init.d/ src/requirements*.txt /tmp/

RUN echo '@edge-community http://dl-cdn.alpinelinux.org/alpine/edge/community' >> /etc/apk/repositories && \
    echo '@edge-testing http://dl-cdn.alpinelinux.org/alpine/edge/testing' >> /etc/apk/repositories && \
    apk add --no-cache $(cat /tmp/apline_apk_list) && \
    mv -v /tmp/ucsschool-id-connector.initd /etc/init.d/ucsschool-id-connector && \
    mv -v /tmp/ucsschool-id-connector-rest-api.initd.final /etc/init.d/ucsschool-id-connector-rest-api && \
    mv -v /tmp/ucsschool-id-connector-rest-api.initd.dev /etc/init.d/ucsschool-id-connector-rest-api-dev && \
    rc-update add ucsschool-id-connector default && \
    rc-update add ucsschool-id-connector-rest-api default && \
    cp -v /usr/share/zoneinfo/Europe/Berlin /etc/localtime && \
    echo "Europe/Berlin" > /etc/timezone && \
    # Disable getty's
    sed -i 's/^\(tty\d\:\:\)/#\1/g' /etc/inittab && \
    sed -i \
        # Change subsystem type to "docker"
        -e 's/#rc_sys=".*"/rc_sys="docker"/g' \
        # Allow all variables through
        -e 's/#rc_env_allow=".*"/rc_env_allow="\*"/g' \
        # Start crashed services
        -e 's/#rc_crashed_stop=.*/rc_crashed_stop=NO/g' \
        -e 's/#rc_crashed_start=.*/rc_crashed_start=YES/g' \
        # Define extra dependencies for services
        -e 's/#rc_provide=".*"/rc_provide="loopback net"/g' \
        /etc/rc.conf \
    # Remove unnecessary services
    && rm -fv /etc/init.d/hwdrivers \
        /etc/init.d/hwclock \
        /etc/init.d/modules \
        /etc/init.d/modules-load \
        /etc/init.d/modloop && \
    # Can't do cgroups
    sed -i 's/\tcgroup_add_service/\t#cgroup_add_service/g' /lib/rc/sh/openrc-run.sh && \
    sed -i 's/VSERVER/DOCKER/Ig' /lib/rc/sh/init.sh && \
    virtualenv --system-site-packages /ucsschool-id-connector/venv && \
    /ucsschool-id-connector/venv/bin/pip3 install --upgrade pip && \
    /ucsschool-id-connector/venv/bin/pip3 install --no-cache-dir -r /tmp/requirements.txt -r /tmp/requirements-dev.txt && \
    rm -rf /root/.cache/ /tmp/* && \
    apk del --no-cache \
        gcc \
        make \
        musl-dev \
        python3-dev

LABEL "description"="UCS@school ID Connector" \
    "version"="$version"

COPY src/ /ucsschool-id-connector/src/

RUN cd /ucsschool-id-connector/src && \
    /ucsschool-id-connector/venv/bin/python3 -m pytest -l -v --color=yes tests/unittests && \
    /ucsschool-id-connector/venv/bin/pip3 install --no-cache-dir --editable . && \
    rst2html5-3 README.rst README.html && \
    rst2html5-3 HISTORY.rst HISTORY.html && \
    rm -rf /ucsschool-id-connector/src/.eggs/ /ucsschool-id-connector/src/.pytest_cache/ /root/.cache/ /tmp/pip*
