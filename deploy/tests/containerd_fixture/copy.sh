set -eu
mkdir /work/source /work/target
printf 'fixture payload\n' > /work/source/file
ln /work/source/file /work/source/hardlink
ln -s file /work/source/symlink
chmod 0640 /work/source/file
chown 12345:12346 /work/source/file
setfacl -m u:23456:r-- /work/source/file
setfattr -n user.blockscout-fixture -v preserved /work/source/file
truncate -s 67108864 /work/source/sparse
printf tail | dd of=/work/source/sparse bs=1 seek=67108860 conv=notrunc 2>/dev/null

# COPY_COMMAND and VERIFY_COMMAND are supplied from the actual executor's
# rsync_command(), with only the two fixture paths substituted.
COPY_COMMAND
VERIFY_COMMAND > /work/differences
test ! -s /work/differences
cmp /work/source/file /work/target/file
test "$(readlink /work/target/symlink)" = file
test "$(stat -c '%i' /work/target/file)" = "$(stat -c '%i' /work/target/hardlink)"
test "$(stat -c '%u:%g:%a' /work/source/file)" = "$(stat -c '%u:%g:%a' /work/target/file)"
test "$(getfattr --only-values -n user.blockscout-fixture /work/target/file 2>/dev/null)" = preserved
getfacl -cp /work/source/file > /work/source.acl
getfacl -cp /work/target/file > /work/target.acl
cmp /work/source.acl /work/target.acl
test "$(stat -c '%s' /work/target/sparse)" = 67108864
test "$(stat -c '%b' /work/target/sparse)" -lt 1024

printf 'corrupted payload\n' > /work/target/file
VERIFY_COMMAND > /work/differences
test -s /work/differences
COPY_COMMAND
printf extra > /work/target/extra
VERIFY_COMMAND > /work/differences
test -s /work/differences
test -f /work/target/extra
printf 'STORAGE_COPY_FIXTURE_OK\n'
