def quicksort(arr):
    # 如果数组长度小于等于1，则数组已经有序，直接返回
    if len(arr) <= 1:
        return arr
    # 选择数组的中间元素作为基准点
    pivot = arr[len(arr) // 2]
    # 将小于基准点的元素放在左边
    left = [x for x in arr if x < pivot]
    # 将等于基准点的元素放在中间
    middle = [x for x in arr if x == pivot]
    # 将大于基准点的元素放在右边
    right = [x for x in arr if x > pivot]
    # 递归地对左边和右边的数组进行排序，并将结果与中间部分合并
    return quicksort(left) + middle + quicksort(right)