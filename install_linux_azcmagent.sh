#!/bin/bash
#
# Copyright (c) Microsoft Corporation.
#
# This script will
#   1.  Configure host machine to download from packages.microsoft.com
#   2.  Install Azcmagent package
#   3.  Configure for proxy operation (if specified on the command line)
#
# Note that this script is for Linux only

proxy=
outfile=
configfile=
altdownloadfile=
format_success=
format_failure=
apt=0
zypper=0
rpm_distro=
deb_distro=
localinstall=0

# Error codes used by azcmagent are in range of [0, 125].
# This bootstrap script will use [127, 255]
# Note that Windows scripts install_azcmagent.ps1 also uses exit codes, and currently uses 146.
function exit_failure {
    if [ -n "${outfile}" ]; then
	json_string=$(printf "$format_failure" "failed" "$1" "$2")
	echo "$json_string" > "$outfile"
    fi
    echo "$2"
    exit 1
}

function exit_success {
    if [ -n "${outfile}" ]; then
	json_string=$(printf "$format_success" "success" "$1")
	echo "$json_string" > "$outfile"
    fi
    echo "$1"
    exit 0
}

function verify_downloadfile {
    if [ -z "${altdownloadfile##*.deb}" ]; then
        if [ $apt -eq 0 ]; then
	    exit_failure 127 "$0: error: altdownload file should not have .deb suffix"
	fi
    elif [ -z "${altdownloadfile##*.rpm}" ]; then
        if [ $apt -eq 1 ]; then
	    exit_failure 128 "$0: error: altdownload file should not have .rpm suffix"
	fi
    else
	if [ $apt -eq 0 ]; then
	    altdownloadfile+=".rpm"
	else
	    altdownloadfile+=".deb"
	fi
    fi
}
	     
# Parse the command-line

while [[ $# -gt 0 ]]
do
key="$1"

case "$key" in
    -p|--proxy)
	proxy="$2"
	shift
	shift
	;;
    -o|--output)
	outfile="$2"
	format_failure='{\n\t"status": "%s",\n\t"error": {\n\t\t"code": "AZCM%04d",\n\t\t"message": "%s"\n\t}\n}'
	format_success='{\n\t"status": "%s",\n\t"message": "%s"\n}'
	shift
	shift
	;;
    -a|--altdownload)
	altdownloadfile="$2"
	shift
	shift
	;;
    -h|--help)
	echo "Usage: $0 [--proxy <proxy>] [--output <output file>] [--altdownload <alternate download file>]"
	echo "For example: $0 --proxy \"localhost:8080\" --output out.json --altdownload http://aka.ms/alternateAzcmagent.deb"
	exit 0
	;;
    *)
	exit_failure 129 "$0: unrecognized argument: '${key}'. Type '$0 --help' for help."
	;;
esac
done

# Make sure we have systemctl in $PATH

if ! [ -x "$(command -v systemctl)" ]; then
    exit_failure 130 "$0: error: Azure Connected Machine Agent requires systemd, and that the command 'systemctl' be found in your PATH"
fi

# Detect OS and Version

__m=$(uname -m 2>/dev/null) || __m=unknown
__s=$(uname -s 2>/dev/null)  || __s=unknown

distro=
distro_version=
case "${__m}:${__s}" in
    x86_64:Linux)
	if [ -f /etc/centos-release ]; then
	   echo "Retrieving distro info from /etc/centos-release..."
           distro=$(awk -F" " '{ print $1 }' /etc/centos-release)
           distro_version=$(awk -F" " '{ print $4 }' /etc/centos-release)
	elif [ -f /etc/os-release ]; then
	   echo "Retrieving distro info from /etc/os-release..."
           distro=$(grep ^NAME /etc/os-release | awk -F"=" '{ print $2 }' | tr -d '"')
           distro_version=$(grep VERSION_ID /etc/os-release | awk -F"=" '{ print $2 }' | tr -d '"')
	elif which lsb_release 2>/dev/null; then
	   echo "Retrieving distro info from lsb_release command..."
           distro=$(lsb_release -i | awk -F":" '{ print $2 }')
           distro_version=$(lsb_release -r | awk -F":" '{ print $2 }')
	else
           exit_failure 131 "$0: error: unknown linux distro"
        fi
        ;;
    *)
        exit_failure 132 "$0: error: unsupported platform: ${__m}:${__s}"
        ;;
esac

distro_major_version=$(echo "${distro_version}" | cut -f1 -d".")
distro_minor_version=$(echo "${distro_version}" | cut -f2 -d".")

# Configuring commands from https://docs.microsoft.com/en-us/windows-server/administration/linux-package-repository-for-microsoft-software

case "${distro}" in
    *edHat* | *ed\ Hat*)
        if [ "${distro_major_version}" -eq 7 ]; then
            echo "Configuring for Redhat 7..."
            rpm_distro=rhel/7
        elif [ "${distro_major_version}" -eq 8 ]; then
            echo "Configuring for Redhat 8..."
            rpm_distro=rhel/8
        else
            exit_failure 133 "$0: error: unsupported Redhat version: ${distro_major_version}:${distro_minor_version}"
        fi
        sudo -E yum -y install curl
        ;;

    *entOS*)
        # Doc says to use RHEL for CentOS: https://docs.microsoft.com/en-us/windows-server/administration/linux-package-repository-for-microsoft-software
        if [ "${distro_major_version}" -eq 7 ]; then
            echo "Configuring for CentOS 7..."
            rpm_distro=rhel/7
            # Yum install on CentOS 7 is not idempotent, and will throw an error if "Nothing to do"
            # The workaround is to use "yum localinstall"
            localinstall=1
        elif [ "${distro_major_version}" -eq 8 ]; then
            echo "Configuring for CentOS 8..."
            rpm_distro=rhel/8
        else
            exit_failure 134 "$0: error: unsupported CentOS version: ${distro_major_version}:${distro_minor_version}"
        fi
        ;;

    *racle*)
        if [ "${distro_major_version}" -eq 7 ]; then
            echo "Configuring for Oracle 7..."
            rpm_distro=rhel/7
        elif [ "${distro_major_version}" -eq 8 ]; then
            echo "Configuring for Oracle 8..."
            rpm_distro=rhel/8
        else
             exit_failure 135 "$0: error: unsupported Oracle version: ${distro_major_version}:${distro_minor_version}"
        fi
        sudo -E yum -y install curl
        ;;

    *SLES*)
        zypper=1
        if [ "${distro_major_version}" -eq 12 ]; then
            echo "Configuring for SLES 12..."
            rpm_distro=sles/12
        elif [ "${distro_major_version}" -eq 15 ]; then
            echo "Configuring for SLES 15..."
	    # As of 3/2020, there is a bug in the sles 15 config file in
	    # download.microsoft.com.  So use the SLES 12 version for now.
            rpm_distro=sles/12
        else
            exit_failure 136 "$0: error: unsupported SLES version: ${distro_major_version}:${distro_minor_version}"
        fi
        sudo -E zypper install -y curl
        ;;

    *mazon\ Linux*)
        if [ "${distro_major_version}" -eq 2 ]; then
            echo "Configuring for Amazon Linux 2 ..."
        else
            exit_failure 137 "$0: error: unsupported Amazon Linux version: ${distro_major_version}:${distro_minor_version}"
        fi

	# Amazon Linux does not exist in packages.microsoft.com currently, so use Redhat 7 instead
	rpm_distro=rhel/7
        sudo -E yum -y install curl
        ;;

    *ebian*)
        exit_failure 138 "$0: error: unsupported platform: Debian"
        sudo -E yum -y install curl
        ;;

    *buntu*)
	apt=1
        if [ "${distro_major_version}" -eq 16 ] && [ "${distro_minor_version}" -eq 04 ]; then
            echo "Configuring for Ubuntu 16.04..."
	    deb_distro=16.04
        elif [ "${distro_major_version}" -eq 18 ] && [ "${distro_minor_version}" -eq 04 ]; then
            echo "Configuring for Ubuntu 18.04..."
	    deb_distro=18.04
        elif [ "${distro_major_version}" -eq 20 ] && [ "${distro_minor_version}" -eq 04 ]; then
            echo "Configuring for Ubuntu 20.04..."
	    deb_distro=20.04
        else
            exit_failure 139 "$0: error: unsupported Ubuntu version: ${distro_major_version}:${distro_minor_version}"
        fi
        sudo -E apt update
        sudo -E apt install -y curl
        ;;        

    *)
        exit_failure 140 "$0: error: unsupported platform: ${distro}"
        ;;
esac


# Install the azcmagent
if [ $apt -eq 1 ]; then
	sudo -E apt install -y ./packages/azcmagent_1.17.01931.118_amd64.deb
elif [ $zypper -eq 1 ]; then
	sudo -E zypper install -y ./packages/azcmagent-1.17.01931-135.x86_64.rpm
else
    sudo -E yum -y localinstall ./packages/azcmagent-1.17.01931-135.x86_64.rpm
fi

if [ $? -ne 0 ]; then
    exit_failure 143 "$0:  installation error"
fi

# Set proxy, if any

if [ -n "${proxy}" ]; then
    echo "Configuring proxy..."
    sudo azcmagent config set proxy.url ${proxy}
fi

exit_success "Latest version of azcmagent is installed."
