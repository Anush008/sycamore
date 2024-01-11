import sys
from sycamore.functions.rabin_karp import RkWindow

__all__ = ["shinglesCalc", "shinglesDist", "simHash", "simHashesDist", "simHashText"]

###############################################################################
#
# Helper Functions
#
###############################################################################


def downHeap(heap, idx):
    """
    downHeap() implements the down-heap operation on a binary max-heap
    represented as a 1-based list.  If the list represents a valid heap
    except that the element at `idx` may be too small, downHeap() will fix
    the heap in log(N) time.
    """

    nn = len(heap) - 1
    limit = nn // 2
    val = heap[idx]

    while idx <= limit:
        kid = 2 * idx
        if (kid < nn) and (heap[kid] < heap[kid + 1]):
            kid += 1
        kidVal = heap[kid]
        if kidVal < val:
            break
        heap[idx] = kidVal
        idx = kid
    heap[idx] = val


def heapUpdate(heap, item):
    """
    heapUpdate() does the equivalent of a pop() and push() if the new
    item is less than the max value in the heap.  This is useful when
    using a max-heap to keep track of the N lowest values.
    """

    if item < heap[1]:
        heap[1] = item
        downHeap(heap, 1)


def scramble(val: int) -> int:
    """
    scramble() takes an existing 64-bit hash value and permutes the bits
    into another hash value.  Uses two special constants:
    6364136223846793005 = f-value for Mersenne Twister MT19937-64
    9223372036854775783 = largest prime < 2^63
    """

    return ((val * 6364136223846793005) + 9223372036854775783) & 0xFFFFFFFFFFFFFFFF


def sortedVectorCmp(aVec: list[int], bVec: list[int]) -> tuple[int, int]:
    """
    sortedVectorCmp() takes two sorted lists and compares their elements.
    The tuple returned is (match_count, max_length).
    """

    aLen = len(aVec)
    bLen = len(bVec)
    aIdx = 0
    bIdx = 0
    matches = 0
    while (aIdx < aLen) and (bIdx < bLen):
        aVal = aVec[aIdx]
        bVal = bVec[bIdx]
        if aVal < bVal:
            aIdx += 1
        elif bVal < aVal:
            bIdx += 1
        else:
            matches += 1
            aIdx += 1
            bIdx += 1
    return (matches, max(aLen, bLen))


###############################################################################
#
# Shingles Functions
#
# The terminology used below is based on basic roofing singles.  A
# standard shingle has 3 "tabs" at the bottom.  These are what is visible
# after the next higher shingle overlaps it.  Each horizonal row of
# shingles is called a "course".
#
#               +--------+
# A standard    |        |
# 3-tab shingle |  |  |  |
#               +--+--+--+
#
#           |  |  |  |
#           +--+--+--+
# 4 courses  |  |  |  |
# of 3-tab   +--+--+--+
# shingles    |  |  |  |
#             +--+--+--+
#              |  |  |  |
#              +--+--+--+
#
# Each box above represents a single hash.  The hashes are made using a
# sliding window.  For example, if the "window" is 5:
#
#   The quick brown fox
#   |___|=>0x1c91
#    |___|=>0xbe8f
#     |___|=>0x9bed
#
# This code uses a hash function that is efficient for sliding windows
# (Rabin-Karp).  The end result is a list of hashes, with length proportional
# to the size of the input.  They are sorted and the first N constitute a
# vertical shingle one-tab wide.  The next tab over is represents the same
# process with a different permutation of the hash values.  Each tab shares
# the same permutation.
#
###############################################################################


def shinglesCalc(text: bytes, window: int = 32, courses: int = 29, tabs: int = 10) -> list[list[int]]:
    """
    shinglesCalc() will process `text` and return a list of variants of
    lists of hashes.  The inner list is often referred to as "shingles"
    and consists for the lowest-value `courses` hashes.  Each top-level
    list represents shingles scrambled `tabs` times.  Conceptually, when
    looking at a section of roof, it's `courses` high and `tabs` wide.
    `window` is the number of bytes in the sliding window that's hashed.
    """

    ww = RkWindow(window)
    heaps = [[0xFFFFFFFFFFFFFFFF for i in range(courses + 1)] for j in range(tabs)]
    for x in text:
        ww.hash(x)
        hh = ww.get()
        if hh is not None:
            for heap in heaps:
                hh = scramble(hh)
                heapUpdate(heap, hh)
    rv = []
    for heap in heaps:
        heap = list(filter(lambda x: x != 0xFFFFFFFFFFFFFFFF, heap))
        nn = len(heap)
        if nn == courses:
            heap.sort()
        elif nn == 0:
            heap = [0] * courses
        else:
            copies = (courses + nn - 1) // nn
            heap *= copies
            heap.sort()
            heap = heap[: courses + 1]
        rv.append(heap)
    return rv


def shinglesDist(aa: list[list[int]], bb: list[list[int]]) -> float:
    """
    shinglesDist() is a distance function for two sets of shingles.
    The outputs of shinglesCalc() can be used here.  The return value
    is a real number [0, 1.0] indicating dissimilarity.
    """

    aLen = len(aa)
    bLen = len(bb)
    assert aLen == bLen
    numer = 0
    denom = 0
    for ii in range(aLen):
        n, d = sortedVectorCmp(aa[ii], bb[ii])
        numer += n
        denom += d
    if denom == 0:
        return 1.0
    return (denom - numer) / denom


###############################################################################
#
# SimHash Functions
#
###############################################################################


def simHash(tab: list[int]) -> int:
    """
    simHash() takes one shingle variant (or "tab"), that is, a vector of
    hashes, and returns a single similarity hash as per Moses Charikar's
    2002 paper.  64-bit is assumed.  For proper results, the number of
    elements in the list should be odd, otherwise the bit distribution
    will be skewed.
    """

    nn = len(tab)
    half = nn // 2
    bit = 0x8000000000000000  # 2^63
    rv = 0
    while bit:
        cnt = 0
        for x in tab:
            if x & bit:
                cnt += 1
        if cnt > half:
            rv |= bit
        bit >>= 1
    return rv


def simHashesDistFast(aa: list[int], bb: list[int]) -> int:
    """
    simHashesDistFast() compares two lists of SimHashes and returns a
    distance metric.  Each list of SimHashes represents a document.
    Corresponding elements in each list represent variants or "tabs" of
    shingles.  With a SimHash, the most bits in common means the most
    similar.  This returns the average of the count of differing bits.
    This fast version for Python >=3.10 takes 50% less time than the slow.
    """

    assert len(aa) == len(bb)
    low = 64
    for a, b in zip(aa, bb):
        x = a ^ b
        pop = x.bit_count()  # type: ignore[attr-defined]
        low = min(low, pop)
    return low


def simHashesDistSlow(aa: list[int], bb: list[int]) -> int:
    """
    simHashesDistSlow() compares two lists of SimHashes and returns a
    distance metric.  Each list of SimHashes represents a document.
    Corresponding elements in each list represent variants or "tabs" of
    shingles.  With a SimHash, the most bits in common means the most
    similar.  This returns the average of the count of differing bits.
    This slow version for Python <=3.9 takes 50% more time than the fast.
    """

    assert len(aa) == len(bb)
    low = 64
    for a, b in zip(aa, bb):
        x = a ^ b
        pop = bin(x).count("1")  # slow way to count set bits
        low = min(low, pop)
    return low


# Python lacks int.bit_count() until version 3.10
if (sys.version_info.major < 3) or ((sys.version_info.major == 3) and (sys.version_info.minor < 10)):
    simHashesDist = simHashesDistSlow
else:
    simHashesDist = simHashesDistFast


def simHashText(text: bytes, window: int = 32, courses: int = 29, tabs: int = 10) -> list[int]:
    """
    Takes text and returns a list of SimHashes.  Arguments:

    text    - The text to process, in UTF-8 bytes
    window  - Width in bytes of the sliding window used for shingles
    courses - The number of least-value shingles to retain
    tabs    - The number of variants of each shingle to process
    """
    assert (courses & 1) == 1
    shingles = shinglesCalc(text, window, courses, tabs)
    return [simHash(hh) for hh in shingles]
