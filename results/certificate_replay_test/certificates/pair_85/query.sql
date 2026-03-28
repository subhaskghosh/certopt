SELECT
  t1.firstname,
  t1.lastname,
  t2.city,
  t2.state
FROM PERSON AS T1
LEFT JOIN ADDRESS AS T2
  ON t1.personid = t2.personid