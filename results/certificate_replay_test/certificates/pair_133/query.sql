SELECT DISTINCT
  t1.email
FROM PERSON AS T1
JOIN PERSON AS T2
  ON t1.id <> t2.id AND t1.email = t2.email