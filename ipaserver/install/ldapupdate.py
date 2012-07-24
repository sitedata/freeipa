# Authors: Rob Crittenden <rcritten@redhat.com>
#
# Copyright (C) 2008  Red Hat
# see file 'COPYING' for use and warranty information
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
#

# Documentation can be found at http://freeipa.org/page/LdapUpdate

# TODO
# save undo files?

UPDATES_DIR="/usr/share/ipa/updates/"

import sys
from ipaserver.install import installutils
from ipaserver.install import service
from ipaserver import ipaldap
from ipapython import entity, ipautil
import uuid
from ipalib import util
from ipalib import errors
from ipalib import api
from ipalib.dn import DN
import ldap
from ldap.dn import escape_dn_chars
from ipapython.ipa_log_manager import *
import krbV
import platform
import time
import random
import os
import pwd
import fnmatch
import csv
import inspect
from ipaserver.install.plugins import PRE_UPDATE, POST_UPDATE
from ipaserver.install.plugins import FIRST, MIDDLE, LAST

class BadSyntax(installutils.ScriptError):
    def __init__(self, value):
        self.value = value
        self.msg = "There is a syntax error in this update file: \n  %s" % value
        self.rval = 1

    def __str__(self):
        return repr(self.value)

class LDAPUpdate:
    def __init__(self, dm_password, sub_dict={}, live_run=True,
                 online=True, ldapi=False, plugins=False):
        """dm_password = Directory Manager password
           sub_dict = substitution dictionary
           live_run = Apply the changes or just test
           online = do an online LDAP update or use an experimental LDIF updater
           ldapi = bind using ldapi. This assumes autobind is enabled.
           plugins = execute the pre/post update plugins
        """
        self.sub_dict = sub_dict
        self.live_run = live_run
        self.dm_password = dm_password
        self.conn = None
        self.modified = False
        self.online = online
        self.ldapi = ldapi
        self.plugins = plugins
        self.pw_name = pwd.getpwuid(os.geteuid()).pw_name

        if sub_dict.get("REALM"):
            self.realm = sub_dict["REALM"]
        else:
            krbctx = krbV.default_context()
            try:
                self.realm = krbctx.default_realm
                suffix = ipautil.realm_to_suffix(self.realm)
            except krbV.Krb5Error:
                self.realm = None
                suffix = None

        domain = ipautil.get_domain_name()
        libarch = self.__identify_arch()

        fqdn = installutils.get_fqdn()
        if fqdn is None:
            raise RuntimeError("Unable to determine hostname")
        fqhn = fqdn # Save this for the sub_dict variable
        if self.ldapi:
            fqdn = "ldapi://%%2fvar%%2frun%%2fslapd-%s.socket" % "-".join(
                self.realm.split(".")
            )

        if not self.sub_dict.get("REALM") and self.realm is not None:
            self.sub_dict["REALM"] = self.realm
        if not self.sub_dict.get("FQDN"):
            self.sub_dict["FQDN"] = fqhn
        if not self.sub_dict.get("DOMAIN"):
            self.sub_dict["DOMAIN"] = domain
        if not self.sub_dict.get("SUFFIX") and suffix is not None:
            self.sub_dict["SUFFIX"] = suffix
        if not self.sub_dict.get("ESCAPED_SUFFIX"):
            self.sub_dict["ESCAPED_SUFFIX"] = escape_dn_chars(suffix)
        if not self.sub_dict.get("LIBARCH"):
            self.sub_dict["LIBARCH"] = libarch
        if not self.sub_dict.get("TIME"):
            self.sub_dict["TIME"] = int(time.time())
        if not self.sub_dict.get("DOMAIN") and domain is not None:
            self.sub_dict["DOMAIN"] = domain

        if online:
            # Try out the connection/password
            try:
                conn = ipaldap.IPAdmin(fqdn, ldapi=self.ldapi, realm=self.realm)
                if self.dm_password:
                    conn.do_simple_bind(binddn="cn=directory manager", bindpw=self.dm_password)
                elif os.getegid() == 0:
                    try:
                        # autobind
                        conn.do_external_bind(self.pw_name)
                    except errors.NotFound:
                        # Fall back
                        conn.do_sasl_gssapi_bind()
                else:
                    conn.do_sasl_gssapi_bind()
                conn.unbind()
            except (ldap.CONNECT_ERROR, ldap.SERVER_DOWN):
                raise RuntimeError("Unable to connect to LDAP server %s" % fqdn)
            except ldap.INVALID_CREDENTIALS:
                raise RuntimeError("The password provided is incorrect for LDAP server %s" % fqdn)
            except ldap.LOCAL_ERROR, e:
                raise RuntimeError('%s' % e.args[0].get('info', '').strip())
        else:
            raise RuntimeError("Offline updates are not supported.")

    # The following 2 functions were taken from the Python
    # documentation at http://docs.python.org/library/csv.html
    def __utf_8_encoder(self, unicode_csv_data):
        for line in unicode_csv_data:
            yield line.encode('utf-8')

    def __unicode_csv_reader(self, unicode_csv_data, quote_char="'", dialect=csv.excel, **kwargs):
        # csv.py doesn't do Unicode; encode temporarily as UTF-8:
        csv_reader = csv.reader(self.__utf_8_encoder(unicode_csv_data),
                                dialect=dialect, delimiter=',',
                                quotechar=quote_char,
                                skipinitialspace=True,
                                **kwargs)
        for row in csv_reader:
            # decode UTF-8 back to Unicode, cell by cell:
            yield [unicode(cell, 'utf-8') for cell in row]

    def __identify_arch(self):
        """On multi-arch systems some libraries may be in /lib64, /usr/lib64,
           etc.  Determine if a suffix is needed based on the current
           architecture.
        """
        bits = platform.architecture()[0]

        if bits == "64bit":
            return "64"
        else:
            return ""

    def _template_str(self, s):
        try:
            return ipautil.template_str(s, self.sub_dict)
        except KeyError, e:
            raise BadSyntax("Unknown template keyword %s" % e)

    def __parse_values(self, line):
        """Parse a comma-separated string into separate values and convert them
           into a list. This should handle quoted-strings with embedded commas
        """
        if   line[0] == "'":
            quote_char = "'"
        else:
            quote_char = '"'
        reader = self.__unicode_csv_reader([line], quote_char)
        value = []
        for row in reader:
            value = value + row
        return value

    def read_file(self, filename):
        if filename == '-':
            fd = sys.stdin
        else:
            fd = open(filename)
        text = fd.readlines()
        if fd != sys.stdin: fd.close()
        return text

    def __entry_to_entity(self, ent):
        """Tne Entry class is a bare LDAP entry. The Entity class has a lot more
           helper functions that we need, so convert to dict and then to Entity.
        """
        entry = dict(ent.data)
        entry['dn'] = ent.dn
        for key,value in entry.iteritems():
            if isinstance(value,list) or isinstance(value,tuple):
                if len(value) == 0:
                    entry[key] = ''
                elif len(value) == 1:
                    entry[key] = value[0]
        return entity.Entity(entry)

    def __combine_updates(self, dn_list, all_updates, update):
        """Combine a new update with the list of total updates

           Updates are stored in 2 lists:
               dn_list: contains a unique list of DNs in the updates
               all_updates: the actual updates that need to be applied

           We want to apply the updates from the shortest to the longest
           path so if new child and parent entries are in different updates
           we can be sure the parent gets written first. This also lets
           us apply any schema first since it is in the very short cn=schema.
        """
        dn = update.get('dn')
        dns = ldap.explode_dn(dn.lower())
        l = len(dns)
        if dn_list.get(l):
            if dn not in dn_list[l]:
                dn_list[l].append(dn)
        else:
            dn_list[l] = [dn]
        if not all_updates.get(dn):
            all_updates[dn] = update
            return all_updates

        e = all_updates[dn]
        if 'default' in update:
            if 'default' in e:
                e['default'] = e['default'] + update['default']
            else:
                e['default'] = update['default']
        elif 'updates' in update:
            if 'updates' in e:
                e['updates'] = e['updates'] + update['updates']
            else:
                e['updates'] = update['updates']
        else:
            root_logger.debug("Unknown key in updates %s" % update.keys())

        all_updates[dn] = e

        return all_updates

    def parse_update_file(self, data, all_updates, dn_list):
        """Parse the update file into a dictonary of lists and apply the update
           for each DN in the file."""
        valid_keywords = ["default", "add", "remove", "only", "deleteentry", "replace", "addifnew", "addifexist"]
        update = {}
        d = ""
        index = ""
        dn = None
        lcount = 0
        for line in data:
            # Strip out \n and extra white space
            lcount = lcount + 1

            # skip comments and empty lines
            line = line.rstrip()
            if line.startswith('#') or line == '': continue

            if line.lower().startswith('dn:'):
                if dn is not None:
                    all_updates = self.__combine_updates(dn_list, all_updates, update)

                update = {}
                dn = line[3:].strip()
                update['dn'] = self._template_str(dn)
            else:
                if dn is None:
                    raise BadSyntax, "dn is not defined in the update"

                line = self._template_str(line)
                if line.startswith(' '):
                    v = d[len(d) - 1]
                    v = v + line[1:]
                    d[len(d) - 1] = v
                    update[index] = d
                    continue
                line = line.strip()
                values = line.split(':', 2)
                if len(values) != 3:
                    raise BadSyntax, "Bad formatting on line %d: %s" % (lcount,line)

                index = values[0].strip().lower()

                if index not in valid_keywords:
                    raise BadSyntax, "Unknown keyword %s" % index

                attr = values[1].strip()
                value = values[2].strip()

                new_value = ""
                if index == "default":
                    new_value = attr + ":" + value
                else:
                    new_value = index + ":" + attr + ":" + value
                    index = "updates"

                d = update.get(index, [])

                d.append(new_value)

                update[index] = d

        if dn is not None:
            all_updates = self.__combine_updates(dn_list, all_updates, update)

        return (all_updates, dn_list)

    def create_index_task(self, attribute):
        """Create a task to update an index for an attribute"""

        # Sleep a bit to ensure previous operations are complete
        if self.live_run:
            time.sleep(5)

        cn_uuid = uuid.uuid1()
        # cn_uuid.time is in nanoseconds, but other users of LDAPUpdate expect
        # seconds in 'TIME' so scale the value down
        self.sub_dict['TIME'] = int(cn_uuid.time/1e9)
        cn = "indextask_%s_%s_%s" % (attribute, cn_uuid.time, cn_uuid.clock_seq)
        dn = DN(('cn', cn), ('cn', 'index'), ('cn', 'tasks'), ('cn', 'config'))

        e = ipaldap.Entry(str(dn))

        e.setValues('objectClass', ['top', 'extensibleObject'])
        e.setValue('cn', cn)
        e.setValue('nsInstance', 'userRoot')
        e.setValues('nsIndexAttribute', attribute)

        root_logger.info("Creating task to index attribute: %s", attribute)
        root_logger.debug("Task id: %s", dn)

        if self.live_run:
            self.conn.addEntry(e)

        return dn

    def monitor_index_task(self, dn):
        """Give a task DN monitor it and wait until it has completed (or failed)
        """

        if not self.live_run:
            # If not doing this live there is nothing to monitor
            return

        # Pause for a moment to give the task time to be created
        time.sleep(1)

        attrlist = ['nstaskstatus', 'nstaskexitcode']
        entry = None

        while True:
            try:
                entry = self.conn.getEntry(dn, ldap.SCOPE_BASE, "(objectclass=*)", attrlist)
            except errors.NotFound, e:
                root_logger.error("Task not found: %s", dn)
                return
            except errors.DatabaseError, e:
                root_logger.error("Task lookup failure %s", e)
                return

            status = entry.getValue('nstaskstatus')
            if status is None:
                # task doesn't have a status yet
                time.sleep(1)
                continue

            if status.lower().find("finished") > -1:
                root_logger.info("Indexing finished")
                break

            root_logger.debug("Indexing in progress")
            time.sleep(1)

        return

    def __create_default_entry(self, dn, default):
        """Create the default entry from the values provided.

           The return type is entity.Entity
        """
        entry = ipaldap.Entry(dn)

        if not default:
            # This means that the entire entry needs to be created with add
            return self.__entry_to_entity(entry)

        for line in default:
            # We already do syntax-parsing so this is safe
            (k, v) = line.split(':',1)
            e = entry.getValues(k)
            if e:
                # multi-valued attribute
                e = list(e)
                e.append(v)
            else:
                e = v
            entry.setValues(k, e)

        return self.__entry_to_entity(entry)

    def __get_entry(self, dn):
        """Retrieve an object from LDAP.

           The return type is ipaldap.Entry
        """
        searchfilter="objectclass=*"
        sattrs = ["*", "aci", "attributeTypes", "objectClasses"]
        scope = ldap.SCOPE_BASE

        return self.conn.getList(dn, scope, searchfilter, sattrs)

    def __update_managed_entries(self):
        """Update and move legacy Managed Entry Plugins."""

        suffix = ipautil.realm_to_suffix(self.realm)
        searchfilter = '(objectclass=*)'
        definitions_managed_entries = []
        old_template_container = 'cn=etc,%s' % suffix
        old_definition_container = 'cn=Managed Entries,cn=plugins,cn=config'
        new = 'cn=Managed Entries,cn=etc,%s' % suffix
        sub = ['cn=Definitions,', 'cn=Templates,']
        new_managed_entries = []
        old_templates = []
        template = None
        try:
            definitions_managed_entries = self.conn.getList(old_definition_container, ldap.SCOPE_ONELEVEL, searchfilter,[])
        except errors.NotFound, e:
            return new_managed_entries
        for entry in definitions_managed_entries:
            new_definition = {}
            definition_managed_entry_updates = {}
            definitions_managed_entries
            old_definition = {'dn': entry.dn, 'deleteentry': ['dn: %s' % entry.dn]}
            old_template = entry.getValue('managedtemplate')
            entry.setValues('managedtemplate', entry.getValue('managedtemplate').replace(old_template_container, sub[1] + new))
            new_definition['dn'] = entry.dn.replace(old_definition_container, sub[0] + new)
            new_definition['default'] = str(entry).strip().replace(': ', ':').split('\n')[1:]
            definition_managed_entry_updates[new_definition['dn']] = new_definition
            definition_managed_entry_updates[old_definition['dn']] = old_definition
            old_templates.append(old_template)
            new_managed_entries.append(definition_managed_entry_updates)
        for old_template in old_templates:
            try:
                template = self.conn.getEntry(old_template, ldap.SCOPE_BASE, searchfilter,[])
                new_template = {}
                template_managed_entry_updates = {}
                old_template = {'dn': template.dn, 'deleteentry': ['dn: %s' % template.dn]}
                new_template['dn'] = template.dn.replace(old_template_container, sub[1] + new)
                new_template['default'] = str(template).strip().replace(': ', ':').split('\n')[1:]
                template_managed_entry_updates[new_template['dn']] = new_template
                template_managed_entry_updates[old_template['dn']] = old_template
                new_managed_entries.append(template_managed_entry_updates)
            except errors.NotFound, e:
                pass
        if len(new_managed_entries) > 0:
            new_managed_entries.sort(reverse=True)

        return new_managed_entries

    def __apply_updates(self, updates, entry):
        """updates is a list of changes to apply
           entry is the thing to apply them to

           Returns the modified entry
        """
        if not updates:
            return entry

        only = {}
        for u in updates:
            # We already do syntax-parsing so this is safe
            (utype, k, values) = u.split(':',2)
            values = self.__parse_values(values)

            e = entry.getValues(k)
            if not isinstance(e, list):
                if e is None:
                    e = []
                else:
                    e = [e]
            for v in values:
                if utype == 'remove':
                    root_logger.debug("remove: '%s' from %s, current value %s", v, k, e)
                    try:
                        e.remove(v)
                    except ValueError:
                        root_logger.warning("remove: '%s' not in %s", v, k)
                        pass
                    entry.setValues(k, e)
                    root_logger.debug('remove: updated value %s', e)
                elif utype == 'add':
                    root_logger.debug("add: '%s' to %s, current value %s", v, k, e)
                    # Remove it, ignoring errors so we can blindly add it later
                    try:
                        e.remove(v)
                    except ValueError:
                        pass
                    e.append(v)
                    root_logger.debug('add: updated value %s', e)
                    entry.setValues(k, e)
                elif utype == 'addifnew':
                    root_logger.debug("addifnew: '%s' to %s, current value %s", v, k, e)
                    # Only add the attribute if it doesn't exist. Only works
                    # with single-value attributes.
                    if len(e) == 0:
                        e.append(v)
                        root_logger.debug('addifnew: set %s to %s', k, e)
                        entry.setValues(k, e)
                elif utype == 'addifexist':
                    root_logger.debug("addifexist: '%s' to %s, current value %s", v, k, e)
                    # Only add the attribute if the entry doesn't exist. We
                    # determine this based on whether it has an objectclass
                    if entry.getValues('objectclass'):
                        e.append(v)
                        root_logger.debug('addifexist: set %s to %s', k, e)
                        entry.setValues(k, e)
                elif utype == 'only':
                    root_logger.debug("only: set %s to '%s', current value %s", k, v, e)
                    if only.get(k):
                        e.append(v)
                    else:
                        e = [v]
                        only[k] = True
                    entry.setValues(k, e)
                    root_logger.debug('only: updated value %s', e)
                elif utype == 'deleteentry':
                    # skip this update type, it occurs in  __delete_entries()
                    return None
                elif utype == 'replace':
                    # v has the format "old::new"
                    try:
                        (old, new) = v.split('::', 1)
                    except ValueError:
                        raise BadSyntax, "bad syntax in replace, needs to be in the format old::new in %s" % v
                    try:
                        e.remove(old)
                        e.append(new)
                        root_logger.debug('replace: updated value %s', e)
                        entry.setValues(k, e)
                    except ValueError:
                        root_logger.debug('replace: %s not found, skipping', old)

                self.print_entity(entry)

        return entry

    def print_entity(self, e, message=None):
        """The entity object currently lacks a str() method"""
        root_logger.debug("---------------------------------------------")
        if message:
            root_logger.debug("%s", message)
        root_logger.debug("dn: " + e.dn)
        attr = e.attrList()
        for a in attr:
            value = e.getValues(a)
            if isinstance(value,str):
                root_logger.debug(a + ": " + value)
            else:
                root_logger.debug(a + ": ")
                for l in value:
                    root_logger.debug("\t" + l)

    def is_schema_updated(self, s):
        """Compare the schema in 's' with the current schema in the DS to
           see if anything has changed. This should account for syntax
           differences (like added parens that make no difference but are
           detected as a change by generateModList()).

           This doesn't handle re-ordering of attributes. They are still
           detected as changes, so foo $ bar != bar $ foo.

           return True if the schema has changed
           return False if it has not
        """
        signature = inspect.getargspec(ldap.schema.SubSchema.__init__)
        if 'check_uniqueness' in signature.args:
            s = ldap.schema.SubSchema(s, check_uniqueness=0)
        else:
            s = ldap.schema.SubSchema(s)
        s = s.ldap_entry()

        # Get a fresh copy and convert into a SubSchema
        n = self.__get_entry("cn=schema")[0]
        n = dict(n.data)
        n = ldap.schema.SubSchema(n)
        n = n.ldap_entry()

        if s == n:
            return False
        else:
            return True

    def __update_record(self, update):
        found = False

        new_entry = self.__create_default_entry(update.get('dn'),
                                                update.get('default'))

        try:
            e = self.__get_entry(new_entry.dn)
            if len(e) > 1:
                # we should only ever get back one entry
                raise BadSyntax, "More than 1 entry returned on a dn search!? %s" % new_entry.dn
            entry = self.__entry_to_entity(e[0])
            found = True
            root_logger.info("Updating existing entry: %s", entry.dn)
        except errors.NotFound:
            # Doesn't exist, start with the default entry
            entry = new_entry
            root_logger.info("New entry: %s", entry.dn)
        except errors.DatabaseError:
            # Doesn't exist, start with the default entry
            entry = new_entry
            root_logger.info("New entry, using default value: %s", entry.dn)

        self.print_entity(entry)

        # Bring this entry up to date
        entry = self.__apply_updates(update.get('updates'), entry)
        if entry is None:
            # It might be None if it is just deleting an entry
            return

        self.print_entity(entry, "Final value")

        if not found:
            # New entries get their orig_data set to the entry itself. We want to
            # empty that so that everything appears new when generating the
            # modlist
            # entry.orig_data = {}
            try:
                if self.live_run:
                    if len(entry.toTupleList()) > 0:
                        # addifexist may result in an entry with only a
                        # dn defined. In that case there is nothing to do.
                        # It means the entry doesn't exist, so skip it.
                        try:
                            self.conn.addEntry(entry)
                        except errors.NotFound:
                            # parent entry of the added entry does not exist
                            # this may not be an error (e.g. entries in NIS container)
                            root_logger.info("Parent DN of %s may not exist, cannot create the entry",
                                    entry.dn)
                            return
                self.modified = True
            except Exception, e:
                root_logger.error("Add failure %s", e)
        else:
            # Update LDAP
            try:
                updated = False
                changes = self.conn.generateModList(entry.origDataDict(), entry.toDict())
                if (entry.dn == "cn=schema"):
                    updated = self.is_schema_updated(entry.toDict())
                else:
                    if len(changes) >= 1:
                        updated = True
                root_logger.debug("%s" % changes)
                root_logger.debug("Live %d, updated %d" % (self.live_run, updated))
                if self.live_run and updated:
                    self.conn.updateEntry(entry.dn, entry.origDataDict(), entry.toDict())
                root_logger.info("Done")
            except errors.EmptyModlist:
                root_logger.info("Entry already up-to-date")
                updated = False
            except errors.DatabaseError, e:
                root_logger.error("Update failed: %s", e)
                updated = False
            except errors.ACIError, e:
                root_logger.error("Update failed: %s", e)
                updated = False

            if ("cn=index" in entry.dn and
                "cn=userRoot" in entry.dn):
                taskid = self.create_index_task(entry.cn)
                self.monitor_index_task(taskid)

            if updated:
                self.modified = True
        return

    def __delete_record(self, updates):
        """
        Run through all the updates again looking for any that should be
        deleted.

        This must use a reversed list so that the longest entries are
        considered first so we don't end up trying to delete a parent
        and child in the wrong order.
        """
        dn = updates['dn']
        deletes = updates.get('deleteentry', [])
        for d in deletes:
            try:
                root_logger.info("Deleting entry %s", dn)
                if self.live_run:
                    self.conn.deleteEntry(dn)
                self.modified = True
            except errors.NotFound, e:
                root_logger.info("%s did not exist:%s", dn, e)
                self.modified = True
            except errors.DatabaseError, e:
                root_logger.error("Delete failed: %s", e)

        updates = updates.get('updates', [])
        for u in updates:
            # We already do syntax-parsing so this is safe
            (utype, k, values) = u.split(':',2)

            if utype == 'deleteentry':
                try:
                    root_logger.info("Deleting entry %s", dn)
                    if self.live_run:
                        self.conn.deleteEntry(dn)
                    self.modified = True
                except errors.NotFound, e:
                    root_logger.info("%s did not exist:%s", dn, e)
                    self.modified = True
                except errors.DatabaseError, e:
                    root_logger.error("Delete failed: %s", e)

        return

    def get_all_files(self, root, recursive=False):
        """Get all update files"""
        f = []
        for path, subdirs, files in os.walk(root):
            for name in files:
                if fnmatch.fnmatch(name, "*.update"):
                    f.append(os.path.join(path, name))
            if not recursive:
                break
        f.sort()
        return f

    def create_connection(self):
        if self.online:
            if self.ldapi:
                self.conn = ipaldap.IPAdmin(ldapi=True, realm=self.realm)
            else:
                self.conn = ipaldap.IPAdmin(self.sub_dict['FQDN'],
                                            ldapi=False,
                                            realm=self.realm)
            try:
                if self.dm_password:
                    self.conn.do_simple_bind(binddn="cn=directory manager", bindpw=self.dm_password)
                elif os.getegid() == 0:
                    try:
                        # autobind
                        self.conn.do_external_bind(self.pw_name)
                    except errors.NotFound:
                        # Fall back
                        self.conn.do_sasl_gssapi_bind()
                else:
                    self.conn.do_sasl_gssapi_bind()
            except ldap.LOCAL_ERROR, e:
                raise RuntimeError('%s' % e.args[0].get('info', '').strip())
        else:
            raise RuntimeError("Offline updates are not supported.")

    def __run_updates(self, dn_list, all_updates):
        # For adds and updates we want to apply updates from shortest
        # to greatest length of the DN. For deletes we want the reverse.
        sortedkeys = dn_list.keys()
        sortedkeys.sort()
        for k in sortedkeys:
            for dn in dn_list[k]:
                self.__update_record(all_updates[dn])

        sortedkeys.reverse()
        for k in sortedkeys:
            for dn in dn_list[k]:
                self.__delete_record(all_updates[dn])

    def update(self, files):
        """Execute the update. files is a list of the update files to use.

           returns True if anything was changed, otherwise False
        """

        updates = None
        if self.plugins:
            root_logger.info('PRE_UPDATE')
            updates = api.Backend.updateclient.update(PRE_UPDATE, self.dm_password, self.ldapi, self.live_run)

        try:
            self.create_connection()
            all_updates = {}
            dn_list = {}
            # Start with any updates passed in from pre-update plugins
            if updates:
                for entry in updates:
                    all_updates.update(entry)
                for upd in updates:
                    for dn in upd:
                        dn_explode = ldap.explode_dn(dn.lower())
                        l = len(dn_explode)
                        if dn_list.get(l):
                            if dn not in dn_list[l]:
                                dn_list[l].append(dn)
                        else:
                            dn_list[l] = [dn]

            for f in files:
                try:
                    root_logger.info("Parsing file %s" % f)
                    data = self.read_file(f)
                except Exception, e:
                    print e
                    sys.exit(e)

                (all_updates, dn_list) = self.parse_update_file(data, all_updates, dn_list)

            self.__run_updates(dn_list, all_updates)
        finally:
            if self.conn: self.conn.unbind()

        if self.plugins:
            root_logger.info('POST_UPDATE')
            updates = api.Backend.updateclient.update(POST_UPDATE, self.dm_password, self.ldapi, self.live_run)
            dn_list = {}
            for upd in updates:
                for dn in upd:
                    dn_explode = ldap.explode_dn(dn.lower())
                    l = len(dn_explode)
                    if dn_list.get(l):
                        if dn not in dn_list[l]:
                            dn_list[l].append(dn)
                    else:
                        dn_list[l] = [dn]
            self.__run_updates(dn_list, updates)

        return self.modified


    def update_from_dict(self, dn_list, updates):
        """
        Apply updates internally as opposed to from a file.

        dn_list is a list of dns to be updated
        updates is a dictionary containing the updates
        """
        if not self.conn:
            self.create_connection()

        self.__run_updates(dn_list, updates)

        return self.modified
