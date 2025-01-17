# -*- coding: utf-8 -*-
# Copyright (c) 2016 Jason Lowe-Power
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are
# met: redistributions of source code must retain the above copyright
# notice, this list of conditions and the following disclaimer;
# redistributions in binary form must reproduce the above copyright
# notice, this list of conditions and the following disclaimer in the
# documentation and/or other materials provided with the distribution;
# neither the name of the copyright holders nor the names of its
# contributors may be used to endorse or promote products derived from
# this software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
# "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
# LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR
# A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT
# OWNER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL,
# SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT
# LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE,
# DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY
# THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
# (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
# OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
#
# Authors: Jason Lowe-Power

import m5
from m5.objects import *
from m5.util import convert
from fs_tools import *
from caches import *

class MySystem(LinuxX86System):

    SimpleOpts.add_option("--no_host_parallel", default=False,
                action="store_true",
                help="Do NOT run gem5 on multiple host threads (kvm only)")

    SimpleOpts.add_option("--cpus", default=2, type="int",
                          help="Number of CPUs in the system")

    SimpleOpts.add_option("--second_disk", default='',
                          help="The second disk image to mount (/dev/hdb)")

    SimpleOpts.add_option("--enable_tuntap", action='store_true',
                           default=False, help="")

    def __init__(self, opts, no_kvm=False):
        super(MySystem, self).__init__()
        self._opts = opts
        self._no_kvm = no_kvm
        self._tuntap = opts.enable_tuntap

        self._host_parallel = not self._opts.no_host_parallel

        # Set up the clock domain and the voltage domain
        self.clk_domain = SrcClockDomain()
        self.clk_domain.clock = '3GHz'
        self.clk_domain.voltage_domain = VoltageDomain()

        mem_size = '8GB'
        self.mem_ranges = [AddrRange('100MB'), # For kernel
                           #AddrRange(0xC0000000, size=0x100000), # For I/0
                           AddrRange(Addr('4GB'), size = mem_size) # All data
                           ]

        # Create the main memory bus
        # This connects to main memory
        self.membus = SystemXBar(width = 64) # 64-byte width
        self.membus.badaddr_responder = BadAddr()
        self.membus.default = Self.badaddr_responder.pio

        # Set up the system port for functional access from the simulator
        self.system_port = self.membus.slave

        self.initFS(self.membus, self._opts.cpus)

        # Replace these paths with the path to your disk images.
        # The first disk is the root disk. The second could be used for swap
        # or anything else.
        imagepath = './fs_stuff/disks/ubuntu-16.04.1-server-amd64.img'
        if opts.second_disk:
            self.setDiskImages(imagepath, opts.second_disk)
        else:
            self.setDiskImages(imagepath, imagepath)

        # Change this path to point to the kernel you want to use
        self.kernel = './fs_stuff/binaries/vmlinux-4.8.13'

        # Options specified on the kernel command line
        boot_options = ['earlyprintk=ttyS0', 'console=ttyS0', 'lpj=7999923',
                        'root=/dev/hda1']
        self.boot_osflags = ' '.join(boot_options)

        # Create the CPUs for our system.
        self.createCPU()

        # Create the cache heirarchy for the system.
        self.createCacheHierarchy()

        # Set up the interrupt controllers for the system (x86 specific)
        self.setupInterrupts()

        self.createMemoryControllersDDR3()

        if self._host_parallel:
            # To get the KVM CPUs to run on different host CPUs
            # Specify a different event queue for each CPU
            for i,cpu in enumerate(self.cpu):
                for obj in cpu.descendants():
                    obj.eventq_index = 0
                cpu.eventq_index = i + 1

    def getHostParallel(self):
        return self._host_parallel

    def totalInsts(self):
        return sum([cpu.totalInsts() for cpu in self.cpu])

    def createCPU(self):
        if self._no_kvm:
            self.cpu = [AtomicSimpleCPU(cpu_id = i, switched_out = False)
                              for i in range(self._opts.cpus)]
            map(lambda c: c.createThreads(), self.cpu)
        else:
            # Note KVM needs a VM and atomic_noncaching
            self.cpu = [X86KvmCPU(cpu_id = i, hostFreq = "3.6GHz")
                        for i in range(self._opts.cpus)]
            map(lambda c: c.createThreads(), self.cpu)
            self.kvm_vm = KvmVM()
            self.mem_mode = 'atomic_noncaching'

            self.atomicCpu = [AtomicSimpleCPU(cpu_id = i,
                                              switched_out = True)
                              for i in range(self._opts.cpus)]
            map(lambda c: c.createThreads(), self.atomicCpu)

        self.timingCpu = [DerivO3CPU(cpu_id = i,
                                     switched_out = True)
                   for i in range(self._opts.cpus)]
        map(lambda c: c.createThreads(), self.timingCpu)

    def switchCpus(self, old, new):
        assert(new[0].switchedOut())
        m5.switchCpus(self, zip(old, new))

    def setDiskImages(self, img_path_1, img_path_2):
        disk0 = CowDisk(img_path_1)
        disk2 = CowDisk(img_path_2)
        self.pc.south_bridge.ide.disks = [disk0, disk2]

    def createCacheHierarchy(self):
        # Create an L3 cache (with crossbar)
        self.l3bus = L2XBar(width = 64,
                            snoop_filter = SnoopFilter(max_capacity='32MB'))

        for cpu in self.cpu:
            # Create a memory bus, a coherent crossbar, in this case
            cpu.l2bus = L2XBar()

            # Create an L1 instruction and data cache
            cpu.icache = L1ICache(self._opts)
            cpu.dcache = L1DCache(self._opts)
            cpu.mmucache = MMUCache()

            # Connect the instruction and data caches to the CPU
            cpu.icache.connectCPU(cpu)
            cpu.dcache.connectCPU(cpu)
            cpu.mmucache.connectCPU(cpu)

            # Hook the CPU ports up to the l2bus
            cpu.icache.connectBus(cpu.l2bus)
            cpu.dcache.connectBus(cpu.l2bus)
            cpu.mmucache.connectBus(cpu.l2bus)

            # Create an L2 cache and connect it to the l2bus
            cpu.l2cache = L2Cache(self._opts)
            cpu.l2cache.connectCPUSideBus(cpu.l2bus)

            # Connect the L2 cache to the L3 bus
            cpu.l2cache.connectMemSideBus(self.l3bus)

        self.l3cache = BankedL3Cache(self._opts)
        self.l3cache.connectCPUSideBus(self.l3bus)

        # Connect the L3 cache to the membus
        self.l3cache.connectMemSideBus(self.membus)

    def setupInterrupts(self):
        for cpu in self.cpu:
            # create the interrupt controller CPU and connect to the membus
            cpu.createInterruptController()

            # For x86 only, connect interrupts to the memory
            # Note: these are directly connected to the memory bus and
            #       not cached
            cpu.interrupts[0].pio = self.membus.master
            cpu.interrupts[0].int_master = self.membus.slave
            cpu.interrupts[0].int_slave = self.membus.master


    def createMemoryControllersDDR3(self):
        self._createMemoryControllers(2, DDR3_1600_8x8)

    def _createMemoryControllers(self, num, cls):
        kernel_controller = self._createKernelMemoryController(cls)

        ranges = self._getInterleaveRanges(self.mem_ranges[-1], num, 7, 20)

        self.mem_cntrls = [
            cls(range = ranges[i],
                port = self.membus.master,
                channels = num)
            for i in range(num)
        ] + [kernel_controller]

    def _createKernelMemoryController(self, cls):
        return cls(range = self.mem_ranges[0],
                   port = self.membus.master,
                   channels = 1)

    def _getInterleaveRanges(self, rng, num, intlv_low_bit, xor_low_bit):
        from math import log
        bits = int(log(num, 2))
        if 2**bits != num:
            m5.fatal("Non-power of two number of memory controllers")

        intlv_bits = bits
        ranges = [
            AddrRange(start=rng.start,
                      end=rng.end,
                      intlvHighBit = intlv_low_bit + intlv_bits - 1,
                      xorHighBit = xor_low_bit + intlv_bits - 1,
                      intlvBits = intlv_bits,
                      intlvMatch = i)
                for i in range(num)
            ]

        return ranges

    def createEthernet(self):
        self.pc.ethernet = IGbE_e1000(pci_bus=0, pci_dev=0, pci_func=0,
                                      InterruptLine=1, InterruptPin=1)
        self.pc.ethernet.pio = self.iobus.master
        self.pc.ethernet.dma = self.iobus.slave
        self.tap = EtherTap()
        self.tap.tap = self.pc.ethernet.interface

    def initFS(self, membus, cpus):
        self.pc = Pc()

        # Constants similar to x86_traits.hh
        IO_address_space_base = 0x8000000000000000
        pci_config_address_space_base = 0xc000000000000000
        interrupts_address_space_base = 0xa000000000000000
        APIC_range_size = 1 << 12;

        # North Bridge
        self.iobus = IOXBar()
        self.bridge = Bridge(delay='50ns')
        self.bridge.master = self.iobus.slave
        self.bridge.slave = membus.master
        # Allow the bridge to pass through:
        #  1) kernel configured PCI device memory map address: address range
        #  [0xC0000000, 0xFFFF0000). (The upper 64kB are reserved for m5ops.)
        #  2) the bridge to pass through the IO APIC (two pages, already
        #     contained in 1),
        #  3) everything in the IO address range up to the local APIC, and
        #  4) then the entire PCI address space and beyond.
        self.bridge.ranges = \
            [
            AddrRange(0xC0000000, 0xFFFF0000),
            AddrRange(IO_address_space_base,
                      interrupts_address_space_base - 1),
            AddrRange(pci_config_address_space_base,
                      Addr.max)
            ]

        # Create a bridge from the IO bus to the memory bus to allow access
        # to the local APIC (two pages)
        self.apicbridge = Bridge(delay='50ns')
        self.apicbridge.slave = self.iobus.master
        self.apicbridge.master = membus.slave
        self.apicbridge.ranges = [AddrRange(interrupts_address_space_base,
                                            interrupts_address_space_base +
                                            cpus * APIC_range_size
                                            - 1)]

        # connect the io bus
        self.pc.attachIO(self.iobus)

        if self._tuntap:
            self.createEthernet()

        # Add a tiny cache to the IO bus.
        # This cache is required for the classic memory model for coherence
        self.iocache = Cache(assoc=8,
                            tag_latency = 50,
                            data_latency = 50,
                            response_latency = 50,
                            mshrs = 20,
                            size = '1kB',
                            tgts_per_mshr = 12,
                            addr_ranges = self.mem_ranges)
        self.iocache.cpu_side = self.iobus.master
        self.iocache.mem_side = self.membus.slave

        self.intrctrl = IntrControl()

        ###############################################

        # Add in a Bios information structure.
        self.smbios_table.structures = [X86SMBiosBiosInformation()]

        # Set up the Intel MP table
        base_entries = []
        ext_entries = []
        for i in range(cpus):
            bp = X86IntelMPProcessor(
                    local_apic_id = i,
                    local_apic_version = 0x14,
                    enable = True,
                    bootstrap = (i ==0))
            base_entries.append(bp)
        io_apic = X86IntelMPIOAPIC(
                id = cpus,
                version = 0x11,
                enable = True,
                address = 0xfec00000)
        self.pc.south_bridge.io_apic.apic_id = io_apic.id
        base_entries.append(io_apic)
        pci_bus = X86IntelMPBus(bus_id = 0, bus_type='PCI   ')
        base_entries.append(pci_bus)
        isa_bus = X86IntelMPBus(bus_id = 1, bus_type='ISA   ')
        base_entries.append(isa_bus)
        connect_busses = X86IntelMPBusHierarchy(bus_id=1,
                subtractive_decode=True, parent_bus=0)
        ext_entries.append(connect_busses)
        pci_dev4_inta = X86IntelMPIOIntAssignment(
                interrupt_type = 'INT',
                polarity = 'ConformPolarity',
                trigger = 'ConformTrigger',
                source_bus_id = 0,
                source_bus_irq = 0 + (4 << 2),
                dest_io_apic_id = io_apic.id,
                dest_io_apic_intin = 16)
        base_entries.append(pci_dev4_inta)

        if self._tuntap:
            # Interrupt assignment for IGbE_e1000 (bus=0,dev=2,fun=0
            pci_dev2_inta = X86IntelMPIOIntAssignment(
                    interrupt_type = 'INT',
                    polarity = 'ConformPolarity',
                    trigger = 'ConformTrigger',
                    source_bus_id = 0,
                    source_bus_irq = 0 + (2 << 2),
                    dest_io_apic_id = io_apic.id,
                    dest_io_apic_intin = 10)
            base_entries.append(pci_dev2_inta)

        def assignISAInt(irq, apicPin):
            assign_8259_to_apic = X86IntelMPIOIntAssignment(
                    interrupt_type = 'ExtInt',
                    polarity = 'ConformPolarity',
                    trigger = 'ConformTrigger',
                    source_bus_id = 1,
                    source_bus_irq = irq,
                    dest_io_apic_id = io_apic.id,
                    dest_io_apic_intin = 0)
            base_entries.append(assign_8259_to_apic)
            assign_to_apic = X86IntelMPIOIntAssignment(
                    interrupt_type = 'INT',
                    polarity = 'ConformPolarity',
                    trigger = 'ConformTrigger',
                    source_bus_id = 1,
                    source_bus_irq = irq,
                    dest_io_apic_id = io_apic.id,
                    dest_io_apic_intin = apicPin)
            base_entries.append(assign_to_apic)
        assignISAInt(0, 2)
        assignISAInt(1, 1)
        for i in range(3, 15):
            assignISAInt(i, i)
        self.intel_mp_table.base_entries = base_entries
        self.intel_mp_table.ext_entries = ext_entries

        entries = \
           [
            # Mark the first megabyte of memory as reserved
            X86E820Entry(addr = 0, size = '639kB', range_type = 1),
            X86E820Entry(addr = 0x9fc00, size = '385kB', range_type = 2),
            # Mark the rest of physical memory as available
            X86E820Entry(addr = 0x100000,
                    size = '%dB' % (self.mem_ranges[0].size() - 0x100000),
                    range_type = 1),
            ]
        # Mark [mem_size, 3GB) as reserved if memory less than 3GB, which
        # force IO devices to be mapped to [0xC0000000, 0xFFFF0000). Requests
        # to this specific range can pass though bridge to iobus.
        entries.append(X86E820Entry(addr = self.mem_ranges[0].size(),
            size='%dB' % (0xC0000000 - self.mem_ranges[0].size()),
            range_type=2))

        # Reserve the last 16kB of the 32-bit address space for m5ops
        entries.append(X86E820Entry(addr = 0xFFFF0000, size = '64kB',
                                    range_type=2))

        # Add the rest of memory. This is where all the actual data is
        entries.append(X86E820Entry(addr = self.mem_ranges[-1].start,
            size='%dB' % (self.mem_ranges[-1].size()),
            range_type=1))

        self.e820_table.entries = entries
