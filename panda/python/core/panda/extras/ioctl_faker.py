import sys
import logging

from panda import ffi

# TODO: only for logger, should probably move it to a separate file
from panda.extras.file_hook import FileHook

# TODO: Ability to fake buffers for specific commands

ioctl_initialized = False
def do_ioctl_init(arch):
	# Default config (x86, x86-64, ARM, AArch 64) with options for PPC
	global ioctl_initialized
	if ioctl_initialized:
		return
	ioctl_initialized = True
	TYPE_BITS = 8
	CMD_BITS = 8
	SIZE_BITS = 14 if arch != "ppc" else 13
	DIR_BITS = 2 if arch != "ppc" else 3
	
	ffi.cdef("""
	struct IoctlCmdBits {
		uint8_t type_num:%d;
		uint8_t cmd_num:%d;
		uint16_t arg_size:%d;
		uint8_t direction:%d;
	};
	
	union IoctlCmdUnion {
		struct IoctlCmdBits bits;
		uint32_t asUnsigned32;
	};
	
	enum ioctl_direction {
		IO = 0,
		IOW = 1,
		IOR = 2,
		IOWR = 3
	};
	""" % (TYPE_BITS, CMD_BITS, SIZE_BITS, DIR_BITS) ,packed=True)

class Ioctl():

    '''
    Unpacked ioctl command with optional buffer
    '''

    def __init__(self, panda, cpu, fd, cmd, guest_ptr, use_osi_linux = False):
        do_ioctl_init(panda.arch)
        self.cmd = ffi.new("union IoctlCmdUnion*")
        self.cmd.asUnsigned32 = cmd
        self.original_ret_code = None
        self.osi = use_osi_linux

        # Optional syscall argument: pointer to buffer
        if (self.cmd.bits.arg_size > 0):
            try:
                self.has_buf = True
                self.guest_ptr = guest_ptr
                self.guest_buf = panda.virtual_memory_read(cpu, self.guest_ptr, self.cmd.bits.arg_size)
            except ValueError:
                raise RuntimeError("Failed to read guest buffer: ioctl({})".format(str(self.cmd)))
        else:
            self.has_buf = False
            self.guest_ptr = None
            self.guest_buf = None

        # Optional OSI usage: process and file name
        if self.osi:
            proc = panda.plugins['osi'].get_current_process(cpu)
            proc_name_ptr = proc.name
            file_name_ptr = panda.plugins['osi_linux'].osi_linux_fd_to_filename(cpu, proc, fd)
            self.proc_name = ffi.string(proc_name_ptr).decode()
            self.file_name = ffi.string(file_name_ptr).decode()
        else:
            self.proc_name = None
            self.file_name = None

    def set_ret_code(self, code):

        self.original_ret_code = code

    def __str__(self):

        if self.osi:
            self_str = "\'{}\' using \'{}\' - ".format(self.proc_name, self.file_name)
        else:
            self_str = ""

        bits = self.cmd.bits
        direction = ffi.string(ffi.cast("enum ioctl_direction", bits.direction))
        ioctl_desc = f"dir={direction},arg_size={bits.arg_size:x},cmd={bits.cmd_num:x},type={bits.type_num:x}"
        if (self.guest_ptr == None):
            self_str += f"ioctl({ioctl_desc}) -> {self.original_ret_code}"
        else:
            self_str += f"ioctl({ioctl_desc},ptr={self.guest_ptr:08x},buf={self.guest_buf}) -> {self.original_ret_code}"
        return self_str

    def __eq__(self, other):

        return (
            self.__class__ == other.__class__ and
            self.cmd.asUnsigned32 == other.cmd.asUnsigned32 and
            self.has_buf == other.has_buf and
            self.guest_ptr == other.guest_ptr and
            self.guest_buf == other.guest_buf and
            self.proc_name == self.proc_name and
            self.file_name == self.file_name
        )

    def __hash__(self):

        return hash((self.cmd.asUnsigned32, self.has_buf, self.guest_ptr, self.guest_buf, self.proc_name, self.file_name))

class IoctlFaker():

    '''
    Interpose ioctl() syscall returns, forcing successes for any failures to simulate missing drivers/peripherals.
    Bin all returns into failures (needed forcing) and successes, store for later retrival/analysis.
    '''

    def __init__(self, panda, use_osi_linux = False):

        self.osi = use_osi_linux
        self._panda = panda
        self._panda.load_plugin("syscalls2")

        if self.osi:
            self._panda.load_plugin("osi")
            self._panda.load_plugin("osi_linux")

        self._logger = logging.getLogger('panda.hooking')
        self._logger.setLevel(logging.DEBUG)

        # Save runtime memory with sets instead of lists (no duplicates)
        self._fail_returns = set()
        self._success_returns = set()

		
        # PPC (other arches use the default config)
        if self._panda.arch == "ppc":
            SIZE_BITS = 13
            DIR_BITS  = 3

        # Force success returns for missing drivers/peripherals
        @self._panda.ppp("syscalls2", "on_sys_ioctl_return")
        def on_sys_ioctl_return(cpu, pc, fd, cmd, arg):

            ioctl = Ioctl(self._panda, cpu, fd, cmd, arg, self.osi)
            ioctl.set_ret_code(self._panda.from_unsigned_guest(cpu.env_ptr.regs[0]))

            if (ioctl.original_ret_code != 0):
                self._fail_returns.add(ioctl)
                cpu.env_ptr.regs[0] = 0
                if ioctl.has_buf:
                    self._logger.warning("Forcing success return for data-containing {}".format(ioctl))
                else:
                    self._logger.info("Forcing success return for data-less {}".format(ioctl))
            else:
                self._success_returns.add(ioctl)

    def _get_returns(self, source, with_buf_only):

        if with_buf_only:
            return list(filter(lambda i: (i.has_buf == True), source))
        else:
            return source

    def get_forced_returns(self, with_buf_only = False):

        return self._get_returns(self._fail_returns, with_buf_only)

    def get_unmodified_returns(self, with_buf_only = False):

        return self._get_returns(self._success_returns, with_buf_only)

if __name__ == "__main__":

    '''
    Bash will issue ioctls on /dev/ttys0 - this is just a simple test to make sure they're being captured
    '''

    from panda import blocking, Panda

    # No arguments, i386. Otherwise argument should be guest arch
    generic_type = sys.argv[1] if len(sys.argv) > 1 else "i386"
    panda = Panda(generic=generic_type)

    def print_list_elems(l):

        if not l:
            print("None")
        else:
            for e in l:
                print(e)

    @blocking
    def run_cmd():

        # Setup faker
        ioctl_faker = IoctlFaker(panda, use_osi_linux=True)

        print("\nRunning \'ls -l\' to ensure ioctl() capture is working...\n")

        # First revert to root snapshot, then type a command via serial
        panda.revert_sync("root")
        panda.run_serial_cmd("cd / && ls -l")

        # Check faker's results
        faked_rets = ioctl_faker.get_forced_returns()
        normal_rets = ioctl_faker.get_unmodified_returns()

        print("{} faked ioctl returns:".format(len(faked_rets)))
        print_list_elems(faked_rets)
        print("\n")

        print("{} normal ioctl returns:".format(len(normal_rets)))
        print_list_elems(normal_rets)
        print("\n")

        # Cleanup
        panda.end_analysis()

    panda.queue_async(run_cmd)
    panda.run()
